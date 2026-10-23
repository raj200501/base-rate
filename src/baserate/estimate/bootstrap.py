"""Cluster-aware resampling weights.

Procedural variants and inserted-violation copies share their source run, so
traces are not independent; every resampling scheme here resamples whole
clusters (source runs), within each class.

- ``multinomial``: the cluster bootstrap. Clusters are drawn with
  replacement; every trace of a drawn cluster gets that cluster's count.
- ``bayesian``: the Bayesian bootstrap (Rubin 1981) at cluster level. Cluster
  weights are Dirichlet(1, ..., 1), scaled to sum to the number of clusters.

Weights are returned as a (B, n) array aligned with the input traces, ready
for :func:`baserate.estimate.operating.budget_recall_batch`.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Literal

import numpy as np
from numpy.typing import NDArray

Kind = Literal["multinomial", "bayesian"]
FloatArray = NDArray[np.float64]


def cluster_index(clusters: Sequence[str] | NDArray[np.str_] | None, n: int) -> NDArray[np.intp]:
    """Map cluster labels to 0..k-1; ``None`` makes every trace its own cluster.

    Args:
        clusters: One label per trace, or None.
        n: Number of traces.

    Returns:
        Integer cluster index per trace.
    """
    if clusters is None:
        return np.arange(n, dtype=np.intp)
    labels = np.asarray(clusters)
    if labels.shape != (n,):
        raise ValueError("need one cluster label per trace")
    _, inverse = np.unique(labels, return_inverse=True)
    return inverse.astype(np.intp)


def cluster_weights(
    index: NDArray[np.intp], reps: int, rng: np.random.Generator, kind: Kind = "multinomial"
) -> FloatArray:
    """Resampling weights per trace for ``reps`` bootstrap replicates.

    Args:
        index: Cluster index per trace (from :func:`cluster_index`).
        reps: Number of replicates B.
        rng: Random generator.
        kind: ``multinomial`` or ``bayesian``.

    Returns:
        Array of shape (reps, n).
    """
    k = int(index.max()) + 1 if index.size else 0
    if k == 0:
        raise ValueError("no traces")
    if kind == "multinomial":
        counts = rng.multinomial(k, np.full(k, 1.0 / k), size=reps).astype(np.float64)
    elif kind == "bayesian":
        counts = rng.dirichlet(np.ones(k), size=reps) * k
    else:
        raise ValueError(f"unknown kind {kind!r}")
    return np.asarray(counts[:, index], dtype=np.float64)


def chunks(reps: int, n: int, max_cells: int = 4_000_000) -> Iterator[int]:
    """Split ``reps`` replicates into chunk sizes whose weight arrays stay small."""
    size = max(1, max_cells // max(n, 1))
    done = 0
    while done < reps:
        step = min(size, reps - done)
        yield step
        done += step


def percentile_interval(samples: NDArray[np.float64], alpha: float = 0.05) -> tuple[float, float]:
    """Equal-tailed percentile interval of bootstrap or posterior samples."""
    if samples.size == 0:
        raise ValueError("no samples")
    lo, hi = np.quantile(samples, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)
