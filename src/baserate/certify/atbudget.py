"""Planning recall-at-budget studies by resampling a pilot.

Recall at a review budget has no closed-form sample size: its interval
depends on the benign tail (which sets the threshold) and on the violation
sample. :func:`budget_power` estimates, by simulation, the probability that a
study with ``n_neg`` benign runs and ``n_pos`` violation runs meets a target:

- ``min_recall``: the lower end of the interval of
  :func:`baserate.estimate.recall_at_budget` is at least ``min_recall``
  (the interval is two-sided at ``alpha``, so its lower end is a one-sided
  bound at ``alpha / 2``), or
- ``half_width``: half the interval width is at most ``half_width``.

Each simulated study draws ``n_neg`` benign and ``n_pos`` violation source
runs with replacement from the pilot (whole runs, with all their traces) and
treats every draw as a new independent run. For the methods that do not
resample (``plugin_*`` and ``cluster_cp``), all simulated studies are
evaluated at once with count weights in
:func:`baserate.estimate.operating.budget_recall_batch`, which equals
evaluating the expanded datasets. The resampling methods call
:func:`baserate.estimate.recall_at_budget` on each expanded dataset.

:func:`plan_budget` evaluates a grid of designs and returns the cheapest one
meeting the power target.

Limitation: a resampled study can only contain pilot scores. The plan is as
good as the pilot's benign tail around the budget threshold
(``BudgetPlan.pilot_tail_neg`` counts pilot benign traces at or above it) and
the pilot's recall, which acts as the true recall.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

from baserate.estimate import intervals
from baserate.estimate.atbudget import (
    BUDGET_METHODS,
    RESAMPLING_METHODS,
    Labels,
    recall_at_budget,
)
from baserate.estimate.bootstrap import chunks, cluster_index
from baserate.estimate.operating import OperatingPoint, budget_point, budget_recall_batch

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]

DEFAULT_N_NEG_GRID: tuple[int, ...] = (1000, 2000, 5000, 10000, 20000, 50000)
"""Default benign-run sizes for :func:`plan_budget`."""

DEFAULT_N_POS_GRID: tuple[int, ...] = (50, 100, 200, 500, 1000)
"""Default violation-run sizes for :func:`plan_budget`."""


@dataclass(frozen=True)
class BudgetDesign:
    """Simulated performance of one study design.

    Attributes:
        n_neg: Benign source runs.
        n_pos: Violation source runs.
        power: Fraction of simulated studies that met the target.
        mcse: Monte Carlo standard error of ``power``.
        median_lower: Median lower end of the recall interval.
        median_upper: Median upper end of the recall interval.
        median_half_width: Median half-width of the recall interval.
        sims: Simulated studies.
    """

    n_neg: int
    n_pos: int
    power: float
    mcse: float
    median_lower: float
    median_upper: float
    median_half_width: float
    sims: int


@dataclass(frozen=True)
class BudgetPlan:
    """Designs evaluated by :func:`plan_budget` and the cheapest one meeting the target.

    Attributes:
        designs: Every evaluated design, in grid order.
        best: Cheapest design with power at least ``power_target`` (ties go
            to higher power); None when no design qualifies.
        pilot: The pilot's own operating point at the budget (its recall
            acts as the true recall in the simulation).
        pilot_tail_neg: Pilot benign traces scoring at or above the pilot threshold.
        method: Interval method of :func:`baserate.estimate.recall_at_budget`.
        alpha: Miss probability of the two-sided interval.
        min_recall: Recall target for the interval's lower end, if used.
        half_width: Half-width target, if used.
        power_target: Requested power.
        seed: Seed of the simulation.
    """

    designs: tuple[BudgetDesign, ...]
    best: BudgetDesign | None
    pilot: OperatingPoint
    pilot_tail_neg: int
    method: str
    alpha: float
    min_recall: float | None
    half_width: float | None
    power_target: float
    seed: int


@dataclass(frozen=True)
class _Pilot:
    """Pilot scores with their run structure."""

    scores: FloatArray
    index: NDArray[np.intp]
    order: NDArray[np.intp]
    starts: NDArray[np.intp]
    sizes: NDArray[np.intp]

    @property
    def runs(self) -> int:
        return int(self.sizes.size)

    @property
    def unclustered(self) -> bool:
        return bool(np.all(self.sizes == 1))


def _pilot(scores: ArrayLike, clusters: Labels) -> _Pilot:
    arr = np.asarray(scores, dtype=np.float64).ravel()
    if arr.size == 0:
        raise ValueError("the pilot needs at least one benign and one violation score")
    idx = cluster_index(clusters, arr.size)
    order = np.argsort(idx, kind="stable").astype(np.intp)
    sizes = np.bincount(idx).astype(np.intp)
    starts = (np.cumsum(sizes) - sizes).astype(np.intp)
    return _Pilot(arr, idx, order, starts, sizes)


def _expand(p: _Pilot, counts: IntArray) -> tuple[FloatArray, IntArray]:
    """Scores and run labels of one simulated study given run draw counts."""
    chosen = np.repeat(np.arange(p.runs), counts)
    sel = p.sizes[chosen]
    total = int(sel.sum())
    first = np.repeat(np.cumsum(sel) - sel, sel)
    offsets = np.repeat(p.starts[chosen], sel) + (np.arange(total) - first)
    labels = np.repeat(np.arange(chosen.size, dtype=np.int64), sel)
    return p.scores[p.order[offsets]], labels


def _check_target(min_recall: float | None, half_width: float | None) -> None:
    if (min_recall is None) == (half_width is None):
        raise ValueError("give exactly one of min_recall and half_width")
    if min_recall is not None and not 0.0 < min_recall < 1.0:
        raise ValueError("min_recall must be in (0, 1)")
    if half_width is not None and not half_width > 0.0:
        raise ValueError("half_width must be positive")


def _simulated_intervals(
    neg: _Pilot,
    pos: _Pilot,
    prevalence: float,
    flag_rate: float,
    n_neg: int,
    n_pos: int,
    method: str,
    alpha: float,
    sims: int,
    reps: int,
    seed: int,
    expand: bool,
) -> tuple[FloatArray, FloatArray]:
    """Lower and upper recall bounds of ``sims`` simulated studies."""
    rng = np.random.default_rng(seed)
    counts_n = rng.multinomial(n_neg, np.full(neg.runs, 1.0 / neg.runs), size=sims)
    counts_p = rng.multinomial(n_pos, np.full(pos.runs, 1.0 / pos.runs), size=sims)
    boot_rng = np.random.default_rng([seed, 1])
    lower = np.empty(sims)
    upper = np.empty(sims)
    if expand or method in RESAMPLING_METHODS:
        for s in range(sims):
            sn, ln = _expand(neg, counts_n[s])
            sp, lp = _expand(pos, counts_p[s])
            est = recall_at_budget(
                sn,
                sp,
                prevalence,
                flag_rate,
                method,
                alpha,
                neg_clusters=None if neg.unclustered else ln,
                pos_clusters=None if pos.unclustered else lp,
                reps=reps,
                rng=boot_rng,
                warn=False,
            )
            lower[s], upper[s] = est.recall.lower, est.recall.upper
        return lower, upper
    done = 0
    for size in chunks(sims, neg.scores.size + pos.scores.size):
        cn = counts_n[done : done + size]
        cp = counts_p[done : done + size]
        recall, _, _ = budget_recall_batch(
            neg.scores,
            pos.scores,
            prevalence,
            flag_rate,
            cn[:, neg.index].astype(np.float64),
            cp[:, pos.index].astype(np.float64),
        )
        if method == "cluster_cp":
            n_eff = np.full(size, float(n_pos))
            lo, hi = intervals.clopper_pearson(recall * n_eff, n_eff, alpha)
        else:
            n_eff = (cp @ pos.sizes).astype(np.float64)
            name = method.removeprefix("plugin_").replace("cp", "clopper_pearson")
            lo, hi = intervals.INTERVALS[name](recall * n_eff, n_eff, alpha)
        lower[done : done + size] = np.minimum(lo, recall)
        upper[done : done + size] = np.maximum(hi, recall)
        done += size
    return lower, upper


def _design(
    neg: _Pilot,
    pos: _Pilot,
    prevalence: float,
    flag_rate: float,
    n_neg: int,
    n_pos: int,
    min_recall: float | None,
    half_width: float | None,
    method: str,
    alpha: float,
    sims: int,
    reps: int,
    seed: int,
) -> BudgetDesign:
    if n_neg < 1 or n_pos < 1:
        raise ValueError("n_neg and n_pos must be at least 1")
    lo, hi = _simulated_intervals(
        neg, pos, prevalence, flag_rate, n_neg, n_pos, method, alpha, sims, reps, seed, False
    )
    half = (hi - lo) / 2
    if min_recall is not None:
        met = lo >= min_recall
    elif half_width is not None:
        met = half <= half_width
    else:  # pragma: no cover - excluded by _check_target
        raise ValueError("no target")
    p = float(met.mean())
    return BudgetDesign(
        n_neg=n_neg,
        n_pos=n_pos,
        power=p,
        mcse=math.sqrt(p * (1 - p) / sims),
        median_lower=float(np.median(lo)),
        median_upper=float(np.median(hi)),
        median_half_width=float(np.median(half)),
        sims=sims,
    )


def _check_common(method: str, sims: int) -> None:
    if method not in BUDGET_METHODS:
        raise ValueError(f"method must be one of {BUDGET_METHODS}")
    if sims < 1:
        raise ValueError("sims must be at least 1")


def budget_power(
    neg: ArrayLike,
    pos: ArrayLike,
    prevalence: float,
    flag_rate: float,
    n_neg: int,
    n_pos: int,
    *,
    min_recall: float | None = None,
    half_width: float | None = None,
    method: str = "composite",
    alpha: float = 0.05,
    neg_clusters: Labels = None,
    pos_clusters: Labels = None,
    sims: int = 200,
    reps: int = 200,
    seed: int = 0,
) -> BudgetDesign:
    """Simulated probability that a study of this size meets the recall target.

    Args:
        neg: Pilot benign scores.
        pos: Pilot violation scores.
        prevalence: Deployment prevalence, in (0, 1).
        flag_rate: Fraction of traffic reviewed, in (0, 1].
        n_neg: Benign source runs in the planned study.
        n_pos: Violation source runs in the planned study.
        min_recall: Target for the interval's lower end (give this or ``half_width``).
        half_width: Target for the interval's half-width.
        method: Interval method, one of :data:`baserate.estimate.BUDGET_METHODS`.
        alpha: Miss probability of the two-sided interval.
        neg_clusters: Source-run label per pilot benign trace (None: independent).
        pos_clusters: Source-run label per pilot violation trace.
        sims: Simulated studies (the Monte Carlo SE of the power is reported).
        reps: Resamples per interval for the resampling methods.
        seed: Seed; the result is deterministic given it.

    Returns:
        The simulated power and interval summaries.
    """
    _check_target(min_recall, half_width)
    _check_common(method, sims)
    return _design(
        _pilot(neg, neg_clusters),
        _pilot(pos, pos_clusters),
        prevalence,
        flag_rate,
        n_neg,
        n_pos,
        min_recall,
        half_width,
        method,
        alpha,
        sims,
        reps,
        seed,
    )


def plan_budget(
    neg: ArrayLike,
    pos: ArrayLike,
    prevalence: float,
    flag_rate: float,
    *,
    min_recall: float | None = None,
    half_width: float | None = None,
    power: float = 0.8,
    n_neg_grid: Sequence[int] = DEFAULT_N_NEG_GRID,
    n_pos_grid: Sequence[int] = DEFAULT_N_POS_GRID,
    neg_cost: float = 1.0,
    pos_cost: float = 1.0,
    method: str = "composite",
    alpha: float = 0.05,
    neg_clusters: Labels = None,
    pos_clusters: Labels = None,
    sims: int = 200,
    reps: int = 200,
    seed: int = 0,
) -> BudgetPlan:
    """Benign and violation runs needed to meet a recall-at-budget target with ``power``.

    Every pair of ``n_neg_grid`` and ``n_pos_grid`` is simulated with
    :func:`budget_power` (same seed for every design, so neighbouring
    designs share random numbers). The best design minimizes
    ``neg_cost * n_neg + pos_cost * n_pos`` among designs whose simulated
    power is at least ``power``.

    Args:
        neg: Pilot benign scores.
        pos: Pilot violation scores.
        prevalence: Deployment prevalence.
        flag_rate: Fraction of traffic reviewed.
        min_recall: Target for the interval's lower end (give this or ``half_width``).
        half_width: Target for the interval's half-width.
        power: Target probability of meeting the target.
        n_neg_grid: Benign run counts to try.
        n_pos_grid: Violation run counts to try.
        neg_cost: Cost of one benign run (for choosing the best design).
        pos_cost: Cost of one violation run.
        method: Interval method of :func:`baserate.estimate.recall_at_budget`.
        alpha: Miss probability of the two-sided interval.
        neg_clusters: Source-run label per pilot benign trace.
        pos_clusters: Source-run label per pilot violation trace.
        sims: Simulated studies per design.
        reps: Resamples per interval for the resampling methods.
        seed: Seed; the result is deterministic given it.

    Returns:
        The evaluated designs and the cheapest one meeting the target.
    """
    _check_target(min_recall, half_width)
    _check_common(method, sims)
    if not 0.0 < power < 1.0:
        raise ValueError("power must be in (0, 1)")
    if not n_neg_grid or not n_pos_grid:
        raise ValueError("the grids must not be empty")
    neg_p, pos_p = _pilot(neg, neg_clusters), _pilot(pos, pos_clusters)
    designs = tuple(
        _design(
            neg_p,
            pos_p,
            prevalence,
            flag_rate,
            int(a),
            int(b),
            min_recall,
            half_width,
            method,
            alpha,
            sims,
            reps,
            seed,
        )
        for a, b in itertools.product(n_neg_grid, n_pos_grid)
    )
    ok = [d for d in designs if d.power >= power]
    best = min(ok, key=lambda d: (neg_cost * d.n_neg + pos_cost * d.n_pos, -d.power), default=None)
    pilot = budget_point(neg_p.scores, pos_p.scores, prevalence, flag_rate)
    return BudgetPlan(
        designs=designs,
        best=best,
        pilot=pilot,
        pilot_tail_neg=int(np.sum(neg_p.scores >= pilot.threshold)),
        method=method,
        alpha=alpha,
        min_recall=min_recall,
        half_width=half_width,
        power_target=power,
        seed=seed,
    )
