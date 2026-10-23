"""Simulation models with exact truths, for validating estimators (experiment E0).

A :class:`ScoreModel` defines the population score distributions of benign
and violation traces. Benign traces may be clustered (procedural variants of
one source run): latent scores follow a Gaussian copula with intra-cluster
correlation ``icc`` on the latent normal scale, so the marginal benign
distribution is unchanged by clustering. :func:`truth` computes the exact
population operating point at a review budget (randomized at ties, as the
estimators are) and the exact fixed-threshold rates at that threshold.

:func:`run_cell` draws replicate datasets for one cell of the E0 grid and
records, for each estimator, whether its interval covered the truth.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy import optimize, stats

from baserate.estimate.atbudget import RESAMPLING_METHODS, recall_at_budget
from baserate.estimate.fixed import fixed_threshold

FloatArray = NDArray[np.float64]

BUDGET_ESTIMATORS: tuple[str, ...] = (
    "budget_recall:plugin_cp",
    "budget_recall:plugin_wilson",
    "budget_recall:plugin_jeffreys",
    "budget_recall:cluster_cp",
    "budget_recall:cluster_bootstrap",
    "budget_recall:bayesian_bootstrap",
    "budget_recall:composite",
)
FIXED_METRICS: tuple[str, ...] = ("fixed_fpr", "fixed_recall", "fixed_precision")
FIXED_INTERVALS: tuple[str, ...] = ("cp", "wilson", "jeffreys", "cluster_cp", "cluster_bootstrap")
ESTIMATORS: tuple[str, ...] = BUDGET_ESTIMATORS + tuple(
    f"{m}:{i}" for m in FIXED_METRICS for i in FIXED_INTERVALS
)


def refits_by_bootstrap(estimator: str) -> bool:
    """Whether an estimator's interval refits by resampling (fewer E0 replicates)."""
    method = estimator.split(":", 1)[1]
    return method in RESAMPLING_METHODS


@dataclass(frozen=True)
class ScoreModel:
    """Population score distributions for benign (0) and violation (1) traces.

    Continuous models give ``neg``/``pos`` as frozen scipy distributions.
    Discrete models give ``cutpoints``: a latent binormal with separation
    ``d`` is cut into ordered levels, which produces ties like rule monitors.
    """

    name: str
    neg: Any = None
    pos: Any = None
    d: float = 0.0
    cutpoints: tuple[float, ...] = ()

    @property
    def discrete(self) -> bool:
        """True for tied, level-valued scores."""
        return bool(self.cutpoints)

    def neg_from_latent(self, z: FloatArray) -> FloatArray:
        """Map standard-normal latent benign values to scores (Gaussian copula)."""
        if self.discrete:
            return self._levels(z)
        assert self.neg is not None
        return np.asarray(self.neg.ppf(stats.norm.cdf(z)), dtype=np.float64)

    def sample_pos(self, rng: np.random.Generator, n: int) -> FloatArray:
        """Independent violation scores."""
        if self.discrete:
            return self._levels(rng.normal(self.d, 1.0, size=n))
        assert self.pos is not None
        return np.asarray(self.pos.rvs(size=n, random_state=rng), dtype=np.float64)

    def _levels(self, z: FloatArray) -> FloatArray:
        k = len(self.cutpoints)
        return np.searchsorted(np.asarray(self.cutpoints), z).astype(np.float64) / k

    def level_probs(self) -> tuple[FloatArray, FloatArray, FloatArray]:
        """(level values descending, benign probs, violation probs) for discrete models."""
        cuts = np.r_[-np.inf, np.asarray(self.cutpoints), np.inf]
        p0 = np.diff(stats.norm.cdf(cuts))
        p1 = np.diff(stats.norm.cdf(cuts - self.d))
        values = np.arange(len(self.cutpoints) + 1, dtype=np.float64) / len(self.cutpoints)
        return values[::-1], p0[::-1], p1[::-1]


MODELS: dict[str, ScoreModel] = {
    "binormal_d1": ScoreModel("binormal_d1", stats.norm(0, 1), stats.norm(1, 1)),
    "binormal_d3": ScoreModel("binormal_d3", stats.norm(0, 1), stats.norm(3, 1)),
    "heavy_tail": ScoreModel("heavy_tail", stats.t(3), stats.norm(5, 1)),
    "discrete4": ScoreModel("discrete4", d=2.0, cutpoints=(0.5, 1.5, 2.5)),
}


@dataclass(frozen=True)
class Design:
    """Sample sizes and clustering of one E0 design."""

    name: str
    n_neg_clusters: int
    neg_cluster_size: int
    n_pos: int
    icc: float = 0.0

    @property
    def n_neg(self) -> int:
        """Total benign traces."""
        return self.n_neg_clusters * self.neg_cluster_size


DESIGNS: dict[str, Design] = {
    "n2000_p100": Design("n2000_p100", 2000, 1, 100),
    "n20000_p500": Design("n20000_p500", 20000, 1, 500),
    "n20000c10_p500": Design("n20000c10_p500", 2000, 10, 500, icc=0.5),
}


@dataclass(frozen=True)
class Truth:
    """Exact population values for one (model, prevalence, flag rate) cell."""

    threshold: float
    budget_recall: float
    fixed_fpr: float
    fixed_recall: float
    fixed_precision: float


def truth(model: ScoreModel, prevalence: float, flag_rate: float) -> Truth:
    """Exact operating point at the budget and fixed-threshold rates at its threshold.

    Fixed-threshold rates count scores at or above the threshold, as
    :func:`baserate.estimate.fixed.fixed_threshold` does.
    """
    if model.discrete:
        values, p0, p1 = model.level_probs()
        mix = prevalence * p1 + (1 - prevalence) * p0
        cum = np.cumsum(mix)
        level = int(np.searchsorted(cum, flag_rate - 1e-15))
        level = min(level, values.size - 1)
        above = cum[level] - mix[level]
        tie = min(max((flag_rate - above) / mix[level], 0.0), 1.0)
        recall = float(p1[:level].sum() + tie * p1[level])
        thr = float(values[level])
        fpr_fixed = float(p0[: level + 1].sum())
        rec_fixed = float(p1[: level + 1].sum())
    else:
        assert model.neg is not None and model.pos is not None
        neg_d, pos_d = model.neg, model.pos

        def gap(t: float) -> float:
            return float(prevalence * pos_d.sf(t) + (1 - prevalence) * neg_d.sf(t) - flag_rate)

        thr = float(optimize.brentq(gap, -60.0, 60.0, xtol=1e-12))
        recall = float(pos_d.sf(thr))
        fpr_fixed = float(neg_d.sf(thr))
        rec_fixed = recall
    denom = prevalence * rec_fixed + (1 - prevalence) * fpr_fixed
    prec = prevalence * rec_fixed / denom if denom > 0 else 0.0
    return Truth(thr, recall, fpr_fixed, rec_fixed, prec)


def sample(
    model: ScoreModel, design: Design, rng: np.random.Generator
) -> tuple[FloatArray, NDArray[np.int64], FloatArray]:
    """One replicate dataset: (benign scores, benign cluster ids, violation scores)."""
    k, m = design.n_neg_clusters, design.neg_cluster_size
    if m > 1:
        u = rng.normal(size=k)
        e = rng.normal(size=(k, m))
        z = (math.sqrt(design.icc) * u[:, None] + math.sqrt(1 - design.icc) * e).ravel()
    else:
        z = rng.normal(size=k)
    clusters = np.repeat(np.arange(k, dtype=np.int64), m)
    return model.neg_from_latent(z), clusters, model.sample_pos(rng, design.n_pos)


def replicate_intervals(
    estimators: tuple[str, ...],
    neg: FloatArray,
    clusters: NDArray[np.int64],
    pos: FloatArray,
    prevalence: float,
    flag_rate: float,
    threshold: float,
    reps: int,
    rng: np.random.Generator,
) -> dict[str, tuple[float, float]]:
    """Intervals of the given estimators on one replicate dataset.

    A fixed-threshold method is fitted once and serves all three fixed metrics.
    """
    cl = clusters if clusters.max() + 1 < clusters.size else None
    out: dict[str, tuple[float, float]] = {}
    fixed_cache: dict[str, Any] = {}
    for est in estimators:
        metric, method = est.split(":", 1)
        if metric == "budget_recall":
            b = recall_at_budget(
                neg,
                pos,
                prevalence,
                flag_rate,
                method,
                neg_clusters=cl,
                reps=reps,
                rng=rng,
                warn=False,
            )
            out[est] = (b.recall.lower, b.recall.upper)
            continue
        if method not in fixed_cache:
            fixed_cache[method] = fixed_threshold(
                neg, pos, threshold, prevalence, method, neg_clusters=cl, reps=reps, rng=rng
            )
        fx = fixed_cache[method]
        iv = {"fixed_fpr": fx.fpr, "fixed_recall": fx.recall, "fixed_precision": fx.precision}
        out[est] = (iv[metric].lower, iv[metric].upper)
    return out


@dataclass(frozen=True)
class CellResult:
    """Coverage record of one estimator in one cell."""

    estimator: str
    hits: NDArray[np.bool_]
    widths: FloatArray


def run_cell(
    model: ScoreModel,
    design: Design,
    prevalence: float,
    flag_rate: float,
    estimators: tuple[str, ...],
    replicates: int,
    bootstrap_replicates: int,
    resamples: int,
    seed: np.random.SeedSequence,
    progress: Callable[[int], None] | None = None,
) -> tuple[Truth, list[CellResult]]:
    """Simulate one grid cell for every estimator on shared replicate data.

    Resampling estimators run on the first ``bootstrap_replicates`` replicates.

    Returns:
        The exact truth and one :class:`CellResult` per estimator.
    """
    tr = truth(model, prevalence, flag_rate)
    data_rng = np.random.default_rng(seed.spawn(1)[0])
    boot_rng = np.random.default_rng(seed.spawn(1)[0])
    hits: dict[str, list[bool]] = {e: [] for e in estimators}
    widths: dict[str, list[float]] = {e: [] for e in estimators}
    for r in range(replicates):
        neg, clusters, pos = sample(model, design, data_rng)
        active = tuple(
            e for e in estimators if r < bootstrap_replicates or not refits_by_bootstrap(e)
        )
        found = replicate_intervals(
            active, neg, clusters, pos, prevalence, flag_rate, tr.threshold, resamples, boot_rng
        )
        for est, (lo, hi) in found.items():
            target = getattr(tr, est.split(":", 1)[0])
            hits[est].append(lo <= target <= hi)
            widths[est].append(hi - lo)
        if progress is not None:
            progress(r)
    return tr, [
        CellResult(e, np.asarray(hits[e], dtype=np.bool_), np.asarray(widths[e], float))
        for e in estimators
    ]


def coverage_passes(covered: int, replicates: int, nominal: float, tolerance: float) -> bool:
    """The E0 criterion: coverage >= nominal - tolerance - 2 * Monte Carlo SE."""
    cov = covered / replicates
    mcse = math.sqrt(cov * (1 - cov) / replicates)
    return cov >= nominal - tolerance - 2 * mcse
