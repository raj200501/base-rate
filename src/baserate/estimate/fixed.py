"""Rates at a fixed decision threshold, with cluster-aware intervals.

When a monitor makes its own decision (a rule fires or not, or a score is
compared with a fixed threshold), recall is the flagged fraction of
violations and the false-positive rate is the flagged fraction of benign
traces. Methods:

- ``cp``, ``wilson``, ``jeffreys``: binomial intervals treating traces as
  independent.
- ``cluster_cp``: conservative exact bounds that respect clustering. The
  upper bound is Clopper-Pearson on the clusters with any flagged trace, the
  lower bound on the clusters with every trace flagged (each one-sided at
  ``alpha / 2``). For exchangeable traces within a cluster these bracket the
  per-trace rate.
- ``cluster_bootstrap``: percentile interval of the cluster-resampled rate.

With zero false positives in ``n`` independent runs the exact 95% upper bound
is about ``3/n``; no method here reports a tighter false-positive rate than
the independent runs support.

Precision at prevalence ``pi`` is ``pi * R / (pi * R + (1 - pi) * F)``; its
interval combines a recall interval and an FPR interval, each at
``alpha / 2`` (Bonferroni), at their unfavourable ends.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

from baserate.estimate import intervals
from baserate.estimate.atbudget import Interval, Labels
from baserate.estimate.bootstrap import (
    chunks,
    cluster_index,
    cluster_weights,
    percentile_interval,
    tail_draw,
)

FIXED_METHODS: tuple[str, ...] = ("cp", "wilson", "jeffreys", "cluster_cp", "cluster_bootstrap")


@dataclass(frozen=True)
class FixedEstimate:
    """Recall, FPR, and precision at a fixed threshold and prevalence."""

    threshold: float
    prevalence: float
    recall: Interval
    fpr: Interval
    precision: Interval
    n_neg: int
    n_pos: int
    n_neg_clusters: int
    n_pos_clusters: int


def rate_interval(
    flags: ArrayLike,
    clusters: Labels = None,
    method: str = "cluster_cp",
    alpha: float = 0.05,
    reps: int = 1000,
    rng: np.random.Generator | None = None,
) -> Interval:
    """Interval for the rate of ``True`` among ``flags``.

    Args:
        flags: Boolean outcome per trace.
        clusters: Source-run label per trace (None: independent).
        method: One of :data:`FIXED_METHODS`.
        alpha: Total miss probability.
        reps: Resamples for ``cluster_bootstrap``.
        rng: Random generator; default seed 0.

    Returns:
        The trace-level rate with its interval.
    """
    if method not in FIXED_METHODS:
        raise ValueError(f"method must be one of {FIXED_METHODS}")
    f = np.asarray(flags, dtype=bool).ravel()
    if f.size == 0:
        raise ValueError("no traces")
    rate = float(f.mean())
    idx = cluster_index(clusters, f.size)
    if method in {"cp", "wilson", "jeffreys"}:
        fn = intervals.INTERVALS["clopper_pearson" if method == "cp" else method]
        lo_a, hi_a = fn(int(f.sum()), f.size, alpha)
        lo, hi = float(lo_a), float(hi_a)
    elif method == "cluster_cp":
        k = int(idx.max()) + 1
        any_flag = np.zeros(k, dtype=bool)
        all_flag = np.ones(k, dtype=bool)
        np.logical_or.at(any_flag, idx, f)
        np.logical_and.at(all_flag, idx, f)
        lo_a, _ = intervals.clopper_pearson(int(all_flag.sum()), k, alpha / 2, side="lower")
        _, hi_a = intervals.clopper_pearson(int(any_flag.sum()), k, alpha / 2, side="upper")
        lo, hi = float(lo_a), float(hi_a)
    else:
        gen = rng if rng is not None else np.random.default_rng(0)
        # only flagged traces need individual weights (exact; see tail_draw)
        drawn = tail_draw(idx, np.ones(f.size), f, reps, gen, "multinomial")
        if drawn is not None:
            values = drawn[0].sum(axis=1) / drawn[1]
        else:  # unequal cluster sizes: draw full weights
            samples: list[NDArray[np.float64]] = []
            for size in chunks(reps, f.size):
                w = cluster_weights(idx, size, gen, "multinomial")
                samples.append((w @ f.astype(np.float64)) / w.sum(axis=1))
            values = np.concatenate(samples)
        lo, hi = percentile_interval(values, alpha)
    return Interval(rate, min(lo, rate), max(hi, rate), method, alpha)


def precision_interval(
    recall: Interval, fpr: Interval, prevalence: float
) -> tuple[float, float, float]:
    """Precision estimate and bounds from recall and FPR intervals.

    Args:
        recall: Recall interval (computed at ``alpha / 2`` for a joint ``alpha``).
        fpr: FPR interval (computed at ``alpha / 2``).
        prevalence: Deployment prevalence.

    Returns:
        (estimate, lower, upper); precision is 0 when nothing is flagged.
    """

    def prec(r: float, f: float) -> float:
        denom = prevalence * r + (1 - prevalence) * f
        return prevalence * r / denom if denom > 0 else 0.0

    return (
        prec(recall.estimate, fpr.estimate),
        prec(recall.lower, fpr.upper),
        prec(recall.upper, fpr.lower),
    )


def fixed_threshold(
    neg: ArrayLike,
    pos: ArrayLike,
    threshold: float,
    prevalence: float,
    method: str = "cluster_cp",
    alpha: float = 0.05,
    neg_clusters: Labels = None,
    pos_clusters: Labels = None,
    reps: int = 1000,
    rng: np.random.Generator | None = None,
) -> FixedEstimate:
    """Recall, FPR, and precision when traces scoring at or above ``threshold`` are flagged.

    Recall and FPR intervals are at ``alpha``; the precision interval spends
    ``alpha / 2`` on each.
    """
    if not 0.0 < prevalence < 1.0:
        raise ValueError("prevalence must be in (0, 1)")
    neg_a = np.asarray(neg, dtype=np.float64).ravel()
    pos_a = np.asarray(pos, dtype=np.float64).ravel()
    gen = rng if rng is not None else np.random.default_rng(0)
    pos_flags, neg_flags = pos_a >= threshold, neg_a >= threshold
    recall = rate_interval(pos_flags, pos_clusters, method, alpha, reps, gen)
    fpr = rate_interval(neg_flags, neg_clusters, method, alpha, reps, gen)
    r_half = rate_interval(pos_flags, pos_clusters, method, alpha / 2, reps, gen)
    f_half = rate_interval(neg_flags, neg_clusters, method, alpha / 2, reps, gen)
    est, lo, hi = precision_interval(r_half, f_half, prevalence)
    return FixedEstimate(
        threshold=threshold,
        prevalence=prevalence,
        recall=recall,
        fpr=fpr,
        precision=Interval(est, min(lo, est), max(hi, est), method, alpha),
        n_neg=int(neg_a.size),
        n_pos=int(pos_a.size),
        n_neg_clusters=int(cluster_index(neg_clusters, neg_a.size).max()) + 1,
        n_pos_clusters=int(cluster_index(pos_clusters, pos_a.size).max()) + 1,
    )
