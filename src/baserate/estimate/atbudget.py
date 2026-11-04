"""Recall and precision at a review budget, with intervals.

:func:`recall_at_budget` is the main estimator of the library. It sets the
threshold so that exactly ``flag_rate`` of deployment traffic is reviewed
(see :mod:`baserate.estimate.operating`), reports recall, precision, and the
false-positive rate there, and attaches an interval by one of these methods:

- ``plugin_cp``, ``plugin_wilson``, ``plugin_jeffreys``: binomial interval on
  the violation sample at the estimated threshold. They ignore uncertainty in
  the threshold itself, which comes from the benign tail.
- ``cluster_cp``: Clopper-Pearson with the number of violation source runs as
  the sample size (a design-effect correction for clustered violations).
- ``cluster_bootstrap``: percentile interval from resampling source runs
  within each class.
- ``bayesian_bootstrap``: equal-tailed interval of the cluster-level Bayesian
  bootstrap.
- ``composite``: threshold uncertainty from resampling benign source runs,
  combined with exact Clopper-Pearson bounds on the violation sample at the
  extreme thresholds; each of the four tails spends a quarter of ``alpha``.

Precision and the false-positive rate at an exact flag rate are monotone
functions of recall, so their intervals map from the recall interval.
Statistical support for each method was validated by simulation (experiment
E0); calls in a configuration where E0 found under-coverage emit
:class:`UnsupportedCellWarning` (see :mod:`baserate.estimate.support`).
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import ArrayLike, NDArray

from baserate.estimate import intervals, support
from baserate.estimate.bootstrap import (
    Kind,
    chunks,
    cluster_index,
    cluster_weights,
    percentile_interval,
    tail_draw,
)
from baserate.estimate.operating import (
    OperatingPoint,
    budget_point,
    budget_recall_batch,
    budget_recall_tail,
    fpr_from_recall,
    precision_from_recall,
)

BUDGET_METHODS: tuple[str, ...] = (
    "plugin_cp",
    "plugin_wilson",
    "plugin_jeffreys",
    "cluster_cp",
    "cluster_bootstrap",
    "bayesian_bootstrap",
    "composite",
)
RESAMPLING_METHODS: frozenset[str] = frozenset(
    {"cluster_bootstrap", "bayesian_bootstrap", "composite"}
)

Labels = Sequence[str] | NDArray[Any] | None


@dataclass(frozen=True)
class Interval:
    """A point estimate with a two-sided interval."""

    estimate: float
    lower: float
    upper: float
    method: str
    alpha: float

    def __post_init__(self) -> None:
        if not self.lower <= self.upper:
            raise ValueError("interval lower end exceeds upper end")


@dataclass(frozen=True)
class BudgetEstimate:
    """Recall, precision, and false-positive rate at a review budget."""

    point: OperatingPoint
    recall: Interval
    precision: Interval
    fpr: Interval
    n_neg: int
    n_pos: int
    n_neg_clusters: int
    n_pos_clusters: int
    warnings: tuple[str, ...] = field(default=())


TAIL_FACTOR = 5.0
"""Initial tail width for resampling, as a multiple of the flag rate."""


def _tail_cutoff(
    neg: NDArray[np.float64],
    pos: NDArray[np.float64],
    neg_base: NDArray[np.float64],
    pos_base: NDArray[np.float64],
    prevalence: float,
    mass: float,
) -> float | None:
    """Score above which the base-weighted mixture holds at least ``mass`` (None: everything)."""
    if mass >= 1.0:
        return None
    scores = np.r_[neg, pos]
    w = np.r_[(1 - prevalence) * neg_base / neg_base.sum(), prevalence * pos_base / pos_base.sum()]
    order = np.argsort(-scores, kind="stable")
    cum = np.cumsum(w[order])
    i = int(np.searchsorted(cum, mass))
    if i >= scores.size - 1:
        return None
    return float(scores[order[i]])


def _resampled_recall(
    neg: NDArray[np.float64],
    pos: NDArray[np.float64],
    prevalence: float,
    flag_rate: float,
    neg_idx: NDArray[np.intp],
    pos_idx: NDArray[np.intp] | None,
    neg_base: NDArray[np.float64],
    pos_base: NDArray[np.float64],
    reps: int,
    rng: np.random.Generator,
    kind: Kind,
) -> NDArray[np.float64]:
    """Recall under ``reps`` cluster resamplings (positives fixed if pos_idx is None).

    Uses the exact tail shortcut of :func:`tail_draw` when it applies,
    widening the tail until every replicate's threshold falls inside it;
    otherwise draws full weight arrays.
    """
    factor = TAIL_FACTOR
    while True:
        cutoff = _tail_cutoff(neg, pos, neg_base, pos_base, prevalence, factor * flag_rate)
        if cutoff is None:
            break
        sel_n, sel_p = neg >= cutoff, pos >= cutoff
        dn = tail_draw(neg_idx, neg_base, sel_n, reps, rng, kind)
        if pos_idx is None:
            dp: tuple[NDArray[np.float64], NDArray[np.float64]] | None = (
                np.broadcast_to(pos_base[sel_p], (reps, int(sel_p.sum()))),
                np.full(reps, pos_base.sum()),
            )
        else:
            dp = tail_draw(pos_idx, pos_base, sel_p, reps, rng, kind)
        if dn is None or dp is None:
            break
        recall, ok = budget_recall_tail(
            neg[sel_n], pos[sel_p], prevalence, flag_rate, dn[0], dp[0], dn[1], dp[1]
        )
        if ok.all():
            return recall
        factor *= 4
    out: list[NDArray[np.float64]] = []
    for size in chunks(reps, neg.size + pos.size):
        wn = cluster_weights(neg_idx, size, rng, kind) * neg_base
        wp = (
            np.broadcast_to(pos_base, (size, pos.size))
            if pos_idx is None
            else cluster_weights(pos_idx, size, rng, kind) * pos_base
        )
        recall, _, _ = budget_recall_batch(neg, pos, prevalence, flag_rate, wn, wp)
        out.append(recall)
    return np.concatenate(out)


def recall_at_budget(
    neg: ArrayLike,
    pos: ArrayLike,
    prevalence: float,
    flag_rate: float,
    method: str = "composite",
    alpha: float = 0.05,
    neg_clusters: Labels = None,
    pos_clusters: Labels = None,
    neg_weights: ArrayLike | None = None,
    pos_weights: ArrayLike | None = None,
    reps: int = 1000,
    rng: np.random.Generator | None = None,
    warn: bool = True,
) -> BudgetEstimate:
    """Estimate recall, precision, and FPR at a review budget with an interval.

    Args:
        neg: Benign scores.
        pos: Violation scores.
        prevalence: Deployment prevalence of violations, in (0, 1).
        flag_rate: Fraction of all traffic reviewed, in (0, 1].
        method: One of :data:`BUDGET_METHODS`.
        alpha: Total miss probability of the two-sided interval.
        neg_clusters: Source-run label per benign trace (None: independent).
        pos_clusters: Source-run label per violation trace (None: independent).
        neg_weights: Optional sampling weights for benign traces.
        pos_weights: Optional sampling weights for violation traces.
        reps: Resamples for the resampling methods.
        rng: Random generator (seeded for reproducibility); default seed 0.
        warn: Emit :class:`UnsupportedCellWarning` for configurations E0 did
            not support.

    Returns:
        The estimate with intervals for recall, precision, and FPR.
    """
    if method not in BUDGET_METHODS:
        raise ValueError(f"method must be one of {BUDGET_METHODS}")
    neg_a = np.asarray(neg, dtype=np.float64).ravel()
    pos_a = np.asarray(pos, dtype=np.float64).ravel()
    gen = rng if rng is not None else np.random.default_rng(0)
    neg_idx = cluster_index(neg_clusters, neg_a.size)
    pos_idx = cluster_index(pos_clusters, pos_a.size)
    n_neg_c, n_pos_c = int(neg_idx.max()) + 1, int(pos_idx.max()) + 1
    neg_base = np.ones(neg_a.size) if neg_weights is None else np.asarray(neg_weights, float)
    pos_base = np.ones(pos_a.size) if pos_weights is None else np.asarray(pos_weights, float)
    point = budget_point(neg_a, pos_a, prevalence, flag_rate, neg_base, pos_base)
    r = point.recall
    if method in {"plugin_cp", "plugin_wilson", "plugin_jeffreys"}:
        fn = intervals.INTERVALS[method.removeprefix("plugin_").replace("cp", "clopper_pearson")]
        lo_a, hi_a = fn(r * pos_a.size, pos_a.size, alpha)
        lo, hi = float(lo_a), float(hi_a)
    elif method == "cluster_cp":
        lo_a, hi_a = intervals.clopper_pearson(r * n_pos_c, n_pos_c, alpha)
        lo, hi = float(lo_a), float(hi_a)
    elif method in {"cluster_bootstrap", "bayesian_bootstrap"}:
        kind: Kind = "multinomial" if method == "cluster_bootstrap" else "bayesian"
        samples = _resampled_recall(
            neg_a,
            pos_a,
            prevalence,
            flag_rate,
            neg_idx,
            pos_idx,
            neg_base,
            pos_base,
            reps,
            gen,
            kind,
        )
        lo, hi = percentile_interval(samples, alpha)
    else:  # composite
        samples = _resampled_recall(
            neg_a,
            pos_a,
            prevalence,
            flag_rate,
            neg_idx,
            None,
            neg_base,
            pos_base,
            reps,
            gen,
            "multinomial",
        )
        q_lo, q_hi = np.quantile(samples, [alpha / 4, 1 - alpha / 4])
        lo_a, _ = intervals.clopper_pearson(q_lo * n_pos_c, n_pos_c, alpha / 4, side="lower")
        _, hi_a = intervals.clopper_pearson(q_hi * n_pos_c, n_pos_c, alpha / 4, side="upper")
        lo, hi = float(lo_a), float(hi_a)
    lo, hi = min(lo, r), max(hi, r)
    rec = Interval(r, lo, hi, method, alpha)
    prec_lo, prec_hi = precision_from_recall([lo, hi], prevalence, flag_rate)
    fpr_lo, fpr_hi = fpr_from_recall([hi, lo], prevalence, flag_rate)
    found = (
        support.check(method, prevalence, flag_rate, neg_a.size, pos_a.size, n_neg_c < neg_a.size)
        if warn
        else ()
    )
    if warn and (neg_weights is not None or pos_weights is not None):
        found = (*found, support.WEIGHTED)
        warnings.warn(
            "sampling weights were passed; weighted designs were not validated in E0",
            support.UnsupportedCellWarning,
            stacklevel=2,
        )
    return BudgetEstimate(
        point=point,
        recall=rec,
        precision=Interval(point.precision, float(prec_lo), float(prec_hi), method, alpha),
        fpr=Interval(point.fpr, float(fpr_lo), float(fpr_hi), method, alpha),
        n_neg=int(neg_a.size),
        n_pos=int(pos_a.size),
        n_neg_clusters=n_neg_c,
        n_pos_clusters=n_pos_c,
        warnings=found,
    )
