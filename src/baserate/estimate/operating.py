"""Operating points at a review budget, with prevalence reweighting.

Deployment setting: a fraction ``prevalence`` of trajectories are violations
and humans review a fixed fraction ``flag_rate`` of all trajectories (for
example 20 per 10,000 is ``flag_rate = 0.002``). The monitor's top-scoring
``flag_rate`` of traffic is reviewed. Labeled data come from a case-control
design: benign and violation traces are sampled separately, so each class is
reweighted to its deployment mass (``1 - prevalence`` and ``prevalence``).

The threshold is randomized at ties: every trace scoring above the threshold
is flagged and a fraction ``tie_fraction`` of the mass exactly at it, so the
flag rate is met exactly even for coarse monitors (rules, ties). Then

- recall = P(flagged | violation),
- precision = prevalence * recall / flag_rate,
- fpr = (flag_rate - prevalence * recall) / (1 - prevalence).

:func:`budget_recall_batch` evaluates many reweightings at once (for the
bootstraps in :mod:`baserate.estimate.bootstrap`).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

FloatArray = NDArray[np.float64]
PER = 10_000


def flag_rate_from_budget(reviews_per_10k: float) -> float:
    """Convert a review budget per 10,000 trajectories to a flag rate."""
    if reviews_per_10k < 0:
        raise ValueError("budget must be non-negative")
    return reviews_per_10k / PER


@dataclass(frozen=True)
class OperatingPoint:
    """A monitor's operating point at a review budget and prevalence."""

    threshold: float
    tie_fraction: float
    flag_rate: float
    prevalence: float
    recall: float
    fpr: float
    precision: float


@dataclass(frozen=True)
class _Levels:
    """Scores grouped into distinct levels, highest first."""

    levels: FloatArray
    neg_order: NDArray[np.intp]
    neg_starts: NDArray[np.intp]
    neg_present: NDArray[np.intp]
    pos_order: NDArray[np.intp]
    pos_starts: NDArray[np.intp]
    pos_present: NDArray[np.intp]


def _validate(prevalence: float, flag_rate: float) -> None:
    if not 0.0 < prevalence < 1.0:
        raise ValueError("prevalence must be in (0, 1)")
    if not 0.0 < flag_rate <= 1.0:
        raise ValueError("flag_rate must be in (0, 1]")


def _levels(neg: FloatArray, pos: FloatArray) -> _Levels:
    if neg.size == 0 or pos.size == 0:
        raise ValueError("need at least one benign and one violation score")
    if not (np.all(np.isfinite(neg)) and np.all(np.isfinite(pos))):
        raise ValueError("scores must be finite")
    levels = np.unique(np.concatenate([neg, pos]))[::-1].astype(np.float64)
    # index of each score's level in the descending level array
    neg_idx = (levels.size - 1 - np.searchsorted(levels[::-1], neg)).astype(np.intp)
    pos_idx = (levels.size - 1 - np.searchsorted(levels[::-1], pos)).astype(np.intp)

    def group(idx: NDArray[np.intp]) -> tuple[NDArray[np.intp], ...]:
        order = np.argsort(idx, kind="stable").astype(np.intp)
        sorted_idx = idx[order]
        starts = np.flatnonzero(np.r_[True, np.diff(sorted_idx) != 0]).astype(np.intp)
        return order, starts, sorted_idx[starts]

    n_order, n_starts, n_present = group(neg_idx)
    p_order, p_starts, p_present = group(pos_idx)
    return _Levels(levels, n_order, n_starts, n_present, p_order, p_starts, p_present)


def _level_mass(
    weights: FloatArray,
    order: NDArray[np.intp],
    starts: NDArray[np.intp],
    present: NDArray[np.intp],
    n_levels: int,
    total: float,
) -> FloatArray:
    """Per-level class mass for each row of ``weights`` (rows sum to ``total``)."""
    sums = weights.sum(axis=1, keepdims=True)
    if np.any(sums <= 0):
        raise ValueError("each weight row needs positive total weight")
    reduced = np.add.reduceat(weights[:, order], starts, axis=1)
    out = np.zeros((weights.shape[0], n_levels))
    out[:, present] = reduced
    return np.asarray(out * (total / sums), dtype=np.float64)


def budget_recall_batch(
    neg: ArrayLike,
    pos: ArrayLike,
    prevalence: float,
    flag_rate: float,
    neg_weights: ArrayLike | None = None,
    pos_weights: ArrayLike | None = None,
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Recall, threshold, and tie fraction at a budget for many weightings.

    Args:
        neg: Benign scores, shape (n0,).
        pos: Violation scores, shape (n1,).
        prevalence: Deployment prevalence of violations.
        flag_rate: Fraction of all traffic that is reviewed.
        neg_weights: Non-negative weights, shape (B, n0) or (n0,); default 1.
        pos_weights: Non-negative weights, shape (B, n1) or (n1,); default 1.

    Returns:
        (recall, threshold, tie_fraction), each of shape (B,).
    """
    _validate(prevalence, flag_rate)
    neg_a = np.asarray(neg, dtype=np.float64).ravel()
    pos_a = np.asarray(pos, dtype=np.float64).ravel()
    lv = _levels(neg_a, pos_a)
    wn = np.atleast_2d(
        np.ones_like(neg_a) if neg_weights is None else np.asarray(neg_weights, float)
    )
    wp = np.atleast_2d(
        np.ones_like(pos_a) if pos_weights is None else np.asarray(pos_weights, float)
    )
    if wn.shape[1] != neg_a.size or wp.shape[1] != pos_a.size:
        raise ValueError("weight columns must match the number of scores")
    if np.any(wn < 0) or np.any(wp < 0):
        raise ValueError("weights must be non-negative")
    b = max(wn.shape[0], wp.shape[0])
    wn = np.broadcast_to(wn, (b, neg_a.size))
    wp = np.broadcast_to(wp, (b, pos_a.size))
    n_levels = lv.levels.size
    mass_n = _level_mass(wn, lv.neg_order, lv.neg_starts, lv.neg_present, n_levels, 1 - prevalence)
    mass_p = _level_mass(wp, lv.pos_order, lv.pos_starts, lv.pos_present, n_levels, prevalence)
    mix = mass_n + mass_p
    cum = np.cumsum(mix, axis=1)
    eps = 1e-12
    # first level at which the cumulative flagged mass reaches the budget
    hit = cum >= flag_rate - eps
    level = np.where(hit.any(axis=1), hit.argmax(axis=1), n_levels - 1)
    rows = np.arange(b)
    at_mix = mix[rows, level]
    above = cum[rows, level] - at_mix
    with np.errstate(divide="ignore", invalid="ignore"):
        tie = np.where(at_mix > 0, (flag_rate - above) / at_mix, 1.0)
    tie = np.clip(tie, 0.0, 1.0)
    cum_p = np.cumsum(mass_p, axis=1)
    at_p = mass_p[rows, level]
    recall = (cum_p[rows, level] - at_p + tie * at_p) / prevalence
    return (
        np.clip(recall, 0.0, 1.0).astype(np.float64),
        lv.levels[level].astype(np.float64),
        tie.astype(np.float64),
    )


def budget_point(
    neg: ArrayLike,
    pos: ArrayLike,
    prevalence: float,
    flag_rate: float,
    neg_weights: ArrayLike | None = None,
    pos_weights: ArrayLike | None = None,
) -> OperatingPoint:
    """The operating point that reviews exactly ``flag_rate`` of traffic.

    Args:
        neg: Benign scores.
        pos: Violation scores.
        prevalence: Deployment prevalence of violations.
        flag_rate: Fraction of all traffic reviewed.
        neg_weights: Optional per-trace weights for benign traces (for
            stratified or importance samples); default 1.
        pos_weights: Optional per-trace weights for violation traces.

    Returns:
        The randomized-threshold operating point.
    """
    nw = None if neg_weights is None else np.asarray(neg_weights, float).reshape(1, -1)
    pw = None if pos_weights is None else np.asarray(pos_weights, float).reshape(1, -1)
    recall, threshold, tie = budget_recall_batch(neg, pos, prevalence, flag_rate, nw, pw)
    r = float(recall[0])
    fpr = max((flag_rate - prevalence * r) / (1 - prevalence), 0.0)
    precision = min(prevalence * r / flag_rate, 1.0)
    return OperatingPoint(
        threshold=float(threshold[0]),
        tie_fraction=float(tie[0]),
        flag_rate=flag_rate,
        prevalence=prevalence,
        recall=r,
        fpr=fpr,
        precision=precision,
    )


def precision_from_recall(recall: ArrayLike, prevalence: float, flag_rate: float) -> FloatArray:
    """Precision at an exact flag rate: ``prevalence * recall / flag_rate`` (capped at 1)."""
    _validate(prevalence, flag_rate)
    r = np.asarray(recall, dtype=np.float64)
    return np.asarray(np.minimum(prevalence * r / flag_rate, 1.0), dtype=np.float64)


def fpr_from_recall(recall: ArrayLike, prevalence: float, flag_rate: float) -> FloatArray:
    """False-positive rate at an exact flag rate (floored at 0)."""
    _validate(prevalence, flag_rate)
    r = np.asarray(recall, dtype=np.float64)
    return np.asarray(
        np.maximum((flag_rate - prevalence * r) / (1 - prevalence), 0.0), dtype=np.float64
    )
