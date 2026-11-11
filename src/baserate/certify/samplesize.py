"""Sample sizes for certifying recall and the false-positive rate.

A claim "FPR <= f" or "recall >= r" is certified at confidence ``1 - alpha``
when the exact one-sided Clopper-Pearson bound of
:func:`baserate.estimate.intervals.clopper_pearson` meets the target: the
upper bound on the false-positive rate is at most ``f``, or the lower bound on
recall is at least ``r``. Every calculator here decides "certified" with that
same function, so a plan and the later analysis cannot disagree. Counts are
of independent units, which are source runs (see :data:`CLUSTER_NOTE`).

Calculators:

- :func:`zero_fp_runs` and :func:`zero_miss_runs`: closed forms for the
  smallest number of runs that certifies the claim when no false positive
  (no miss) occurs: ``n = ceil(ln(alpha) / ln(1 - f))`` and
  ``n = ceil(ln(alpha) / ln(r))``. At 95% the first is about ``3 / f``.
- :func:`events_allowed`: the most false positives (or misses) that ``n``
  runs can contain and still certify.
- :func:`fpr_power` and :func:`recall_power`: the exact probability of
  certifying with ``n`` runs under an assumed true rate.
- :func:`fpr_runs_for_power` and :func:`recall_runs_for_power`: runs needed
  to certify with a target power (exact binomial search; see below).
- :func:`joint_plan`: both claims at one overall confidence.
- :func:`more_runs_fpr` and :func:`more_runs_recall`: how many more runs an
  existing claim needs.

**Power search rule.** Power is not monotone in ``n`` (the sawtooth of exact
binomial tests): each time the number of allowed events steps up, power
jumps, and between steps it drifts down as ``n`` grows. The smallest ``n``
with power at the target can be followed by ``n + 1`` below it. The planners
therefore return the smallest ``n`` from which power stays at or above the
target at every ``n`` through a checked horizon of at least twice that ``n``
(``RunPlan.runs``), and report the first crossing separately
(``RunPlan.first_runs``). Planning at the first crossing is unsafe: a few
extra runs added for convenience could lower the power below target.

**Adding runs after a look.** :func:`more_runs_fpr` and
:func:`more_runs_recall` report when the bound on all runs together (old and
new) would certify. That bound treats the total number of runs as fixed in
advance. When runs are added because an earlier look at the same claim did
not certify, the claim is tested twice, and the probability of certifying a
false claim can exceed ``alpha`` (tools/validate_certify.py measures it). To
keep ``alpha``, fix the total before looking, or certify on the new runs
alone (planned with the calculators above) at the part of ``alpha`` that
earlier looks did not spend.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy import stats

from baserate.estimate import intervals

Metric = Literal["fpr", "recall"]
IntArray = NDArray[np.int64]
FloatArray = NDArray[np.float64]
BoolArray = NDArray[np.bool_]

DEFAULT_N_MAX = 10_000_000
"""Default cap on the number of runs a planner searches."""

_CAP = 2**60
"""Search cap for the exact run counts (far beyond any real design)."""

CLUSTER_NOTE = (
    "Units are independent source runs: n_neg benign runs and n_pos violation runs. "
    "Procedural variants of one run are not independent. With several traces per run, "
    "count run-level events as estimate.fixed's cluster_cp does: a benign run is a false "
    "positive if any of its traces is flagged (bounds the FPR from above), and a violation "
    "run is detected only if all of its traces are flagged (bounds recall from below). "
    "Assumed rates must then be these run-level rates. estimate.fixed.rate_interval with "
    "method='cluster_cp' spends alpha/2 on each end, so pass alpha = 2 * alpha_arm to it "
    "to reproduce a one-sided certificate at alpha_arm."
)


@dataclass(frozen=True)
class RunPlan:
    """Runs needed to certify one claim.

    Attributes:
        metric: ``"fpr"`` (claim FPR <= bound) or ``"recall"`` (claim recall >= bound).
        bound: The target: the largest FPR or the smallest recall to certify.
        alpha: Miss probability of the one-sided bound.
        runs: Independent runs to collect.
        events_allowed: The most false positives (or misses) among ``runs``
            runs that still certify.
        assumed: Assumed true FPR (or recall); None for a zero-event plan.
        power: Exact probability of certifying at ``runs`` under ``assumed``.
        power_target: Requested power; None for a zero-event plan.
        first_runs: The first ``n`` with power at the target (power may dip
            below it afterwards); None for a zero-event plan.
        horizon: Largest ``n`` checked by the stays-above rule.
        min_power: Smallest power over ``runs .. horizon``.
    """

    metric: Metric
    bound: float
    alpha: float
    runs: int
    events_allowed: int
    assumed: float | None = None
    power: float | None = None
    power_target: float | None = None
    first_runs: int | None = None
    horizon: int | None = None
    min_power: float | None = None


@dataclass(frozen=True)
class JointPlan:
    """Runs needed to certify recall >= r and FPR <= f together.

    Attributes:
        min_recall: Recall target r.
        max_fpr: FPR target f.
        alpha: Overall miss probability (confidence ``1 - alpha``).
        split: ``"bonferroni"`` or ``"sidak"``.
        alpha_recall: Miss probability spent on the recall bound.
        alpha_fpr: Miss probability spent on the FPR bound.
        n_pos: Independent violation runs.
        n_neg: Independent benign runs.
        recall: Plan for the recall claim.
        fpr: Plan for the FPR claim.
        joint_power: Probability that both claims certify, when both rates
            were assumed (the two samples are independent, so it is the
            product of the arm powers); otherwise None.
        power_target: Requested joint power; None when no rate was assumed.
        note: How the units map to clustered data.
    """

    min_recall: float
    max_fpr: float
    alpha: float
    split: str
    alpha_recall: float
    alpha_fpr: float
    n_pos: int
    n_neg: int
    recall: RunPlan
    fpr: RunPlan
    joint_power: float | None
    power_target: float | None
    note: str = CLUSTER_NOTE


@dataclass(frozen=True)
class MoreRuns:
    """Additional runs an existing claim needs.

    Attributes:
        metric: ``"fpr"`` or ``"recall"``.
        events: False positives (or misses) observed so far.
        runs: Independent runs observed so far.
        bound: The target (largest FPR or smallest recall).
        alpha: Miss probability of the one-sided bound.
        current_bound: The current one-sided bound (upper for FPR, lower for recall).
        certified: Whether the current data already certify the claim.
        observed: The observed FPR (``events / runs``) or recall.
        if_no_events: Additional runs needed if none of them has an event.
        at_observed_rate: Additional runs needed if events continue at the
            observed rate (expected counts); None when unreachable.
        reachable: False when the observed rate is at or beyond the target,
            so the bound cannot meet it at that rate however many runs are added.
        for_power: Additional runs after which the probability of certifying
            stays at or above ``power_target`` when future events are
            binomial at the observed rate (stays-above rule); None when not
            requested or unreachable.
        power_target: Requested power, if any.
    """

    metric: Metric
    events: int
    runs: int
    bound: float
    alpha: float
    current_bound: float
    certified: bool
    observed: float
    if_no_events: int
    at_observed_rate: int | None
    reachable: bool
    for_power: int | None = None
    power_target: float | None = None


def _check_open_unit(name: str, value: float) -> None:
    if not 0.0 < value < 1.0:
        raise ValueError(f"{name} must be in (0, 1)")


def _check_alpha(alpha: float) -> None:
    """Alpha in (0, 1), and large enough that the exact upper bound is computable.

    The upper bound evaluates a Beta quantile at ``1 - alpha``; when that
    rounds to 1 in double precision (alpha below about 1.1e-16) the bound is 1
    for every sample size, so no plan could certify.
    """
    _check_open_unit("alpha", alpha)
    if 1.0 - alpha == 1.0:
        raise ValueError("alpha is too small for exact bounds in double precision")


def _certifies(
    metric: Metric, events: ArrayLike, runs: ArrayLike, bound: float, alpha: float
) -> BoolArray:
    """Whether ``events`` false positives (misses) in ``runs`` runs certify the claim.

    For recall, ``events`` counts misses and the lower bound is computed on
    the detections ``runs - events``. ``events`` may be non-integer (expected counts).
    """
    ev = np.asarray(events, dtype=np.float64)
    n = np.asarray(runs, dtype=np.float64)
    if metric == "fpr":
        _, hi = intervals.clopper_pearson(ev, n, alpha, side="upper")
        return np.asarray(hi <= bound, dtype=np.bool_)
    lo, _ = intervals.clopper_pearson(np.maximum(n - ev, 0.0), n, alpha, side="lower")
    return np.asarray(lo >= bound, dtype=np.bool_)


def _first_true(pred: Callable[[IntArray], BoolArray], lo: IntArray, cap: int) -> IntArray:
    """Elementwise smallest ``n >= lo`` with ``pred(n)``, for predicates monotone in ``n``.

    Values below ``lo`` are taken to be False. Elements with no solution up to
    ``cap`` are returned as -1.
    """
    lo = np.maximum(np.asarray(lo, dtype=np.int64), 1)
    hi = lo.copy()
    ok = pred(hi)
    # double until pred holds (pred is False below lo throughout)
    while True:
        grow = ~ok & (hi < cap)
        if not grow.any():
            break
        lo = np.where(grow, hi + 1, lo)
        hi = np.where(grow, np.where(hi > cap // 2, cap, hi * 2), hi)
        ok = pred(hi)
    lo = np.where(ok, lo, hi)
    while np.any(lo < hi):
        mid = (lo + hi) // 2
        hit = pred(mid)
        hi = np.where(hit, mid, hi)
        lo = np.where(hit, lo, mid + 1)
    return np.where(ok, hi, -1).astype(np.int64)


def _min_runs(metric: Metric, events: IntArray, bound: float, alpha: float, cap: int) -> IntArray:
    """Smallest number of runs at which ``events`` events still certify (-1 above ``cap``)."""
    ev = np.asarray(events, dtype=np.int64)
    return _first_true(lambda n: _certifies(metric, ev, n, bound, alpha), ev + 1, cap)


def _event_bound(metric: Metric, bound: float) -> float:
    """The target as a bound on the event rate (FPR, or miss rate ``1 - recall``)."""
    return bound if metric == "fpr" else 1.0 - bound


def _zero_event_runs(metric: Metric, bound: float, alpha: float) -> int:
    _check_alpha(alpha)
    _check_open_unit("max_fpr" if metric == "fpr" else "min_recall", bound)
    denom = math.log1p(-bound) if metric == "fpr" else math.log(bound)
    n = max(math.ceil(math.log(alpha) / denom), 1)
    if bool(_certifies(metric, 0, n, bound, alpha)) and (
        n == 1 or not bool(_certifies(metric, 0, n - 1, bound, alpha))
    ):
        return n
    # rounding at an exact integer moved the answer off the closed form: search
    # with the decision function itself (bounded, unlike a step-by-step walk)
    found = int(_min_runs(metric, np.zeros(1, dtype=np.int64), bound, alpha, cap=_CAP)[0])
    if found < 0:
        raise ValueError(f"no number of runs up to {_CAP} certifies this target")
    return found


def zero_fp_runs(max_fpr: float, alpha: float = 0.05) -> int:
    """Smallest ``n`` such that zero false positives in ``n`` runs certify FPR <= ``max_fpr``.

    Closed form ``ceil(ln(alpha) / ln(1 - max_fpr))``, about ``3 / max_fpr``
    at alpha = 0.05; checked against the exact bound
    ``1 - alpha**(1/n)`` of :func:`baserate.estimate.intervals.zero_event_upper`.

    Args:
        max_fpr: Largest false-positive rate to certify, in (0, 1).
        alpha: Miss probability of the one-sided bound (0.05 for 95%).

    Returns:
        The number of independent benign runs.
    """
    return _zero_event_runs("fpr", max_fpr, alpha)


def zero_miss_runs(min_recall: float, alpha: float = 0.05) -> int:
    """Smallest ``n`` such that detecting all of ``n`` runs certifies recall >= ``min_recall``.

    Closed form ``ceil(ln(alpha) / ln(min_recall))``: the exact lower bound
    after ``n`` detections in ``n`` runs is ``alpha**(1/n)``.

    Args:
        min_recall: Smallest recall to certify, in (0, 1).
        alpha: Miss probability of the one-sided bound.

    Returns:
        The number of independent violation runs.
    """
    return _zero_event_runs("recall", min_recall, alpha)


def events_allowed(
    runs: ArrayLike, bound: float, metric: Metric = "fpr", alpha: float = 0.05
) -> IntArray:
    """The most events among ``runs`` runs for which the claim still certifies.

    Events are false positives for ``metric="fpr"`` and misses for
    ``metric="recall"``.

    Args:
        runs: Number of independent runs (scalar or array, each >= 1).
        bound: Largest FPR or smallest recall to certify, in (0, 1).
        metric: ``"fpr"`` or ``"recall"``.
        alpha: Miss probability of the one-sided bound.

    Returns:
        Integer array shaped like ``runs``; -1 where even zero events do not certify.
    """
    _check_alpha(alpha)
    _check_open_unit("bound", bound)
    n = np.asarray(runs, dtype=np.int64)
    if np.any(n < 1):
        raise ValueError("runs must be at least 1")
    if n.size == 0:
        return n.copy()
    top = int(n.max())
    # the bound exceeds events / runs, so at most bound * runs events can certify
    k_max = math.floor(_event_bound(metric, bound) * top) + 2
    nk = _min_runs(metric, np.arange(k_max + 1, dtype=np.int64), bound, alpha, cap=_CAP)
    # -1 marks event counts that no run count up to the cap certifies
    nk = np.where(nk < 0, np.iinfo(np.int64).max, nk)
    return (np.searchsorted(nk, n, side="right") - 1).astype(np.int64)


def _power(
    runs: ArrayLike, bound: float, assumed: float, metric: Metric, alpha: float
) -> FloatArray:
    n = np.asarray(runs, dtype=np.int64)
    m = events_allowed(n, bound, metric, alpha)
    p_event = assumed if metric == "fpr" else 1.0 - assumed
    return np.asarray(stats.binom.cdf(m, n, p_event), dtype=np.float64)


def fpr_power(runs: ArrayLike, max_fpr: float, true_fpr: float, alpha: float = 0.05) -> FloatArray:
    """Exact probability that ``runs`` runs certify FPR <= ``max_fpr``.

    False positives are Binomial(runs, true_fpr); the claim certifies when
    their count is at most :func:`events_allowed`.

    Args:
        runs: Number of independent benign runs (scalar or array).
        max_fpr: FPR target, in (0, 1).
        true_fpr: Assumed true FPR, in [0, 1].
        alpha: Miss probability of the one-sided bound.

    Returns:
        Power, shaped like ``runs``.
    """
    if not 0.0 <= true_fpr <= 1.0:
        raise ValueError("true_fpr must be in [0, 1]")
    return _power(runs, max_fpr, true_fpr, "fpr", alpha)


def recall_power(
    runs: ArrayLike, min_recall: float, true_recall: float, alpha: float = 0.05
) -> FloatArray:
    """Exact probability that ``runs`` violation runs certify recall >= ``min_recall``.

    Args:
        runs: Number of independent violation runs (scalar or array).
        min_recall: Recall target, in (0, 1).
        true_recall: Assumed true recall, in [0, 1].
        alpha: Miss probability of the one-sided bound.

    Returns:
        Power, shaped like ``runs``.
    """
    if not 0.0 <= true_recall <= 1.0:
        raise ValueError("true_recall must be in [0, 1]")
    return _power(runs, min_recall, true_recall, "recall", alpha)


@dataclass(frozen=True)
class _Stable:
    runs: int
    first: int
    horizon: int
    power: float
    min_power: float


def _stable_search(
    power_of: Callable[[IntArray], FloatArray],
    start: int,
    guess: int,
    target: float,
    n_max: int,
) -> _Stable | None:
    """Smallest ``n >= start`` from which ``power_of`` stays >= ``target`` through the horizon.

    The horizon doubles until it is at least twice the answer. Returns None
    when no such ``n`` exists within ``n_max``.
    """
    horizon = min(max(2 * max(guess, start), start + 64), n_max)
    while True:
        n = np.arange(start, horizon + 1, dtype=np.int64)
        pw = power_of(n)
        ok = pw >= target
        if ok.any():
            fails = np.flatnonzero(~ok)
            i = 0 if fails.size == 0 else int(fails[-1]) + 1
            if i < n.size and 2 * int(n[i]) <= horizon:
                return _Stable(
                    runs=int(n[i]),
                    first=int(n[int(np.argmax(ok))]),
                    horizon=int(horizon),
                    power=float(pw[i]),
                    min_power=float(pw[i:].min()),
                )
        if horizon >= n_max:
            return None
        horizon = min(2 * horizon, n_max)


def _normal_guess(b_event: float, p_event: float, alpha: float, power: float) -> int:
    """Normal-approximation sample size, used only to seed the exact search."""
    if p_event <= 0.0:
        return 1
    za = float(stats.norm.ppf(1 - alpha))
    zb = float(stats.norm.ppf(power))
    num = za * math.sqrt(b_event * (1 - b_event)) + zb * math.sqrt(p_event * (1 - p_event))
    return max(1, math.ceil((num / (b_event - p_event)) ** 2))


def _zero_plan(metric: Metric, bound: float, alpha: float) -> RunPlan:
    return RunPlan(metric, bound, alpha, _zero_event_runs(metric, bound, alpha), 0)


def _power_plan(
    metric: Metric, bound: float, assumed: float, power: float, alpha: float, n_max: int
) -> RunPlan:
    _check_open_unit("power", power)
    n0 = _zero_event_runs(metric, bound, alpha)
    b_event = _event_bound(metric, bound)
    p_event = assumed if metric == "fpr" else 1.0 - assumed
    better = assumed < bound if metric == "fpr" else assumed > bound
    if not (0.0 <= assumed <= 1.0 and better):
        side = "below max_fpr" if metric == "fpr" else "above min_recall"
        raise ValueError(f"the assumed true rate must be in [0, 1] and strictly {side}")
    guess = _normal_guess(b_event, p_event, alpha, power)
    found = _stable_search(
        lambda n: _power(n, bound, assumed, metric, alpha), n0, guess, power, n_max
    )
    if found is None:
        raise ValueError(f"power {power} is not reached and kept within n_max={n_max} runs")
    return RunPlan(
        metric=metric,
        bound=bound,
        alpha=alpha,
        runs=found.runs,
        events_allowed=int(events_allowed(found.runs, bound, metric, alpha)),
        assumed=assumed,
        power=found.power,
        power_target=power,
        first_runs=found.first,
        horizon=found.horizon,
        min_power=found.min_power,
    )


def fpr_runs_for_power(
    max_fpr: float,
    true_fpr: float,
    power: float = 0.8,
    alpha: float = 0.05,
    n_max: int = DEFAULT_N_MAX,
) -> RunPlan:
    """Benign runs needed to certify FPR <= ``max_fpr`` with probability ``power``.

    Exact binomial search with the stays-above rule of this module's
    docstring: ``runs`` is the smallest ``n`` from which the power under
    Binomial(n, true_fpr) stays at or above ``power`` through the horizon.

    Args:
        max_fpr: FPR target, in (0, 1).
        true_fpr: Assumed true FPR, in [0, max_fpr).
        power: Target probability of certifying.
        alpha: Miss probability of the one-sided bound.
        n_max: Largest number of runs searched.

    Returns:
        The plan; raises ValueError if the power is not reached within ``n_max``.
    """
    return _power_plan("fpr", max_fpr, true_fpr, power, alpha, n_max)


def recall_runs_for_power(
    min_recall: float,
    true_recall: float,
    power: float = 0.8,
    alpha: float = 0.05,
    n_max: int = DEFAULT_N_MAX,
) -> RunPlan:
    """Violation runs needed to certify recall >= ``min_recall`` with probability ``power``.

    Same rule as :func:`fpr_runs_for_power`, with misses Binomial(n, 1 - true_recall).

    Args:
        min_recall: Recall target, in (0, 1).
        true_recall: Assumed true recall, in (min_recall, 1].
        power: Target probability of certifying.
        alpha: Miss probability of the one-sided bound.
        n_max: Largest number of runs searched.

    Returns:
        The plan; raises ValueError if the power is not reached within ``n_max``.
    """
    return _power_plan("recall", min_recall, true_recall, power, alpha, n_max)


def split_alpha(
    alpha: float, split: str = "bonferroni", recall_share: float = 0.5
) -> tuple[float, float]:
    """Split an overall miss probability between the recall and FPR bounds.

    ``bonferroni`` gives ``alpha * share`` and ``alpha * (1 - share)``; it is
    valid whatever the dependence between the two samples. ``sidak`` gives
    ``1 - (1 - alpha)**share`` and ``1 - (1 - alpha)**(1 - share)``, slightly
    larger, and is valid because the violation and benign samples are
    independent.

    Args:
        alpha: Overall miss probability.
        split: ``"bonferroni"`` (default) or ``"sidak"``.
        recall_share: Share of ``alpha`` spent on the recall bound, in (0, 1).

    Returns:
        (alpha_recall, alpha_fpr).
    """
    _check_alpha(alpha)
    _check_open_unit("recall_share", recall_share)
    if split == "bonferroni":
        return alpha * recall_share, alpha * (1.0 - recall_share)
    if split == "sidak":
        return 1.0 - (1.0 - alpha) ** recall_share, 1.0 - (1.0 - alpha) ** (1.0 - recall_share)
    raise ValueError("split must be 'bonferroni' or 'sidak'")


def joint_plan(
    min_recall: float,
    max_fpr: float,
    alpha: float = 0.05,
    *,
    true_recall: float | None = None,
    true_fpr: float | None = None,
    power: float = 0.8,
    split: str = "bonferroni",
    recall_share: float = 0.5,
    n_max: int = DEFAULT_N_MAX,
) -> JointPlan:
    """Runs needed to certify recall >= ``min_recall`` and FPR <= ``max_fpr`` together.

    The overall confidence ``1 - alpha`` is split between the two one-sided
    bounds by :func:`split_alpha` (Bonferroni by default). An arm without an
    assumed true rate is planned at its zero-event size (it certifies only if
    no event occurs). Arms with an assumed rate are planned for power: when
    both rates are assumed, each arm targets ``power ** 0.5`` so that the
    joint probability of certifying both (a product, the samples being
    independent) is at least ``power``; when only one is assumed, that arm
    targets ``power``.

    Args:
        min_recall: Recall target, in (0, 1).
        max_fpr: FPR target, in (0, 1).
        alpha: Overall miss probability (0.05 for 95% confidence).
        true_recall: Assumed true (run-level) recall, or None.
        true_fpr: Assumed true (run-level) FPR, or None.
        power: Target joint probability of certifying.
        split: ``"bonferroni"`` or ``"sidak"``.
        recall_share: Share of ``alpha`` spent on recall.
        n_max: Largest number of runs searched per arm.

    Returns:
        The joint plan, with ``n_pos`` violation runs and ``n_neg`` benign runs.
    """
    a_r, a_f = split_alpha(alpha, split, recall_share)
    n_assumed = (true_recall is not None) + (true_fpr is not None)
    if n_assumed:
        # validate before taking a root: a negative power would give a complex arm power
        _check_open_unit("power", power)
    arm_power = power ** (1.0 / n_assumed) if n_assumed else None
    rec = (
        _zero_plan("recall", min_recall, a_r)
        if true_recall is None or arm_power is None
        else _power_plan("recall", min_recall, true_recall, arm_power, a_r, n_max)
    )
    fpr = (
        _zero_plan("fpr", max_fpr, a_f)
        if true_fpr is None or arm_power is None
        else _power_plan("fpr", max_fpr, true_fpr, arm_power, a_f, n_max)
    )
    joint = rec.power * fpr.power if rec.power is not None and fpr.power is not None else None
    return JointPlan(
        min_recall=min_recall,
        max_fpr=max_fpr,
        alpha=alpha,
        split=split,
        alpha_recall=a_r,
        alpha_fpr=a_f,
        n_pos=rec.runs,
        n_neg=fpr.runs,
        recall=rec,
        fpr=fpr,
        joint_power=joint,
        power_target=power if n_assumed else None,
    )


def _more_runs(
    metric: Metric,
    events: int,
    runs: int,
    bound: float,
    alpha: float,
    power: float | None,
    n_max: int,
) -> MoreRuns:
    _check_alpha(alpha)
    _check_open_unit("bound", bound)
    if runs < 1 or not 0 <= events <= runs:
        raise ValueError("need runs >= 1 and 0 <= events <= runs")
    if power is not None:
        _check_open_unit("power", power)
    if metric == "fpr":
        _, hi = intervals.clopper_pearson(events, runs, alpha, side="upper")
        current, observed = float(hi), events / runs
    else:
        lo, _ = intervals.clopper_pearson(runs - events, runs, alpha, side="lower")
        current, observed = float(lo), (runs - events) / runs
    certified = bool(_certifies(metric, events, runs, bound, alpha))
    need = int(_min_runs(metric, np.array([events]), bound, alpha, cap=_CAP)[0])
    add_a = max(need, runs) - runs
    rate = events / runs
    reachable = observed < bound if metric == "fpr" else observed > bound
    add_b: int | None
    if events == 0:
        add_b = add_a
    elif not reachable:
        add_b = None
    else:
        total = _first_true(
            lambda n: _certifies(metric, rate * n, n, bound, alpha),
            np.array([runs], dtype=np.int64),
            cap=n_max + runs,
        )[0]
        add_b = None if total < 0 else int(total) - runs
    add_c: int | None = None
    if power is not None and add_b is not None:
        if certified or rate == 0.0:
            add_c = add_a
        else:

            def power_of(m: IntArray) -> FloatArray:
                room = events_allowed(runs + m, bound, metric, alpha) - events
                return np.asarray(stats.binom.cdf(room, m, rate), dtype=np.float64)

            found = _stable_search(power_of, add_a, 2 * add_b, power, n_max)
            add_c = None if found is None else found.runs
    return MoreRuns(
        metric=metric,
        events=events,
        runs=runs,
        bound=bound,
        alpha=alpha,
        current_bound=current,
        certified=certified,
        observed=observed,
        if_no_events=add_a,
        at_observed_rate=add_b,
        reachable=reachable or events == 0,
        for_power=add_c,
        power_target=power,
    )


def more_runs_fpr(
    false_positives: int,
    runs: int,
    max_fpr: float,
    alpha: float = 0.05,
    power: float | None = None,
    n_max: int = DEFAULT_N_MAX,
) -> MoreRuns:
    """Additional benign runs needed before FPR <= ``max_fpr`` certifies.

    Reports the additional runs (a) if none of them has a false positive and
    (b) if false positives continue at the observed rate ``k / n`` (expected
    counts, so the event count may be fractional), or that (b) is
    unreachable because ``k / n >= max_fpr``. With ``power``, also (c) the
    additional runs from which the probability of certifying stays at or
    above ``power`` when future false positives are Binomial at ``k / n``.
    The answers assume one analysis of all runs; see "Adding runs after a
    look" in the module docstring before extending a study that failed to
    certify.

    Args:
        false_positives: Runs with a false positive so far (k).
        runs: Independent benign runs so far (n >= 1).
        max_fpr: FPR target, in (0, 1).
        alpha: Miss probability of the one-sided bound.
        power: Optional target probability for (c).
        n_max: Largest number of additional runs searched for (b) and (c).

    Returns:
        The current bound and the three answers.
    """
    return _more_runs("fpr", false_positives, runs, max_fpr, alpha, power, n_max)


def more_runs_recall(
    detected: int,
    runs: int,
    min_recall: float,
    alpha: float = 0.05,
    power: float | None = None,
    n_max: int = DEFAULT_N_MAX,
) -> MoreRuns:
    """Additional violation runs needed before recall >= ``min_recall`` certifies.

    Same answers as :func:`more_runs_fpr`, with misses as the events: (a) all
    additional runs are detected, (b) misses continue at the observed rate,
    unreachable when the observed recall is at or below ``min_recall``, and
    (c) the stays-above power rule. The same caveat on adding runs after a
    look applies.

    Args:
        detected: Violation runs detected so far.
        runs: Independent violation runs so far (n >= 1).
        min_recall: Recall target, in (0, 1).
        alpha: Miss probability of the one-sided bound.
        power: Optional target probability for (c).
        n_max: Largest number of additional runs searched for (b) and (c).

    Returns:
        The current bound and the three answers; ``events`` counts misses.
    """
    if not 0 <= detected <= runs:
        raise ValueError("need 0 <= detected <= runs")
    return _more_runs("recall", runs - detected, runs, min_recall, alpha, power, n_max)
