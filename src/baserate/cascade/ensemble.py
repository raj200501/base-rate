"""Score ensembles of several monitors (separate from cascades).

Monitors score on different scales, so each monitor's scores are first
mapped to ranks against a reference sample (:func:`rank_normalize`) and the
normalized scores are then combined by mean or max (:func:`combine`).
:func:`ensemble_scores` does both for benign and violation traces at once.

Reference choice: ``"pooled"`` (default) ranks against the monitor's benign
and violation scores together; the map is strictly increasing on observed
values, so no two distinct scores of one monitor merge. ``"benign"`` ranks
against benign scores only, which does not depend on the case-control mix
but maps every score above the largest benign score to 1, merging the far
tail where low review budgets operate. For held-out evaluation, normalize
test scores against a training reference.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray

FloatArray = NDArray[np.float64]
How = Literal["mean", "max"]
Reference = Literal["pooled", "benign"]


def rank_normalize(scores: ArrayLike, reference: ArrayLike) -> FloatArray:
    """Mid-rank empirical CDF of ``reference`` evaluated at each score.

    A score maps to ``(#reference below it + 0.5 * #reference equal to it) /
    #reference``, a value in [0, 1].

    Args:
        scores: Scores to normalize.
        reference: Reference sample (finite).

    Returns:
        Normalized scores, shaped like ``scores``.
    """
    ref = np.sort(np.asarray(reference, dtype=np.float64).ravel())
    if ref.size == 0 or not np.all(np.isfinite(ref)):
        raise ValueError("reference must be non-empty and finite")
    s = np.asarray(scores, dtype=np.float64)
    below = np.searchsorted(ref, s, side="left")
    upto = np.searchsorted(ref, s, side="right")
    return np.asarray((below + 0.5 * (upto - below)) / ref.size, dtype=np.float64)


def combine(normalized: Sequence[ArrayLike], how: How = "mean") -> FloatArray:
    """Combine aligned normalized scores of several monitors trace by trace.

    Args:
        normalized: One array per monitor, all of the same length.
        how: ``"mean"`` or ``"max"``.

    Returns:
        The ensemble score per trace.
    """
    if not normalized:
        raise ValueError("need at least one monitor")
    stack = np.vstack([np.asarray(a, dtype=np.float64).ravel() for a in normalized])
    if how == "mean":
        return np.asarray(stack.mean(axis=0), dtype=np.float64)
    if how == "max":
        return np.asarray(stack.max(axis=0), dtype=np.float64)
    raise ValueError("how must be 'mean' or 'max'")


def ensemble_scores(
    neg: Sequence[ArrayLike],
    pos: Sequence[ArrayLike],
    how: How = "mean",
    reference: Reference = "pooled",
) -> tuple[FloatArray, FloatArray]:
    """Rank-normalized mean or max ensemble of several monitors.

    Args:
        neg: Benign scores, one array per monitor (aligned across monitors).
        pos: Violation scores, one array per monitor (aligned across monitors).
        how: ``"mean"`` or ``"max"``.
        reference: ``"pooled"`` or ``"benign"`` (see the module docstring).

    Returns:
        (benign ensemble scores, violation ensemble scores).
    """
    if len(neg) != len(pos) or not neg:
        raise ValueError("need the same positive number of monitors for both classes")
    if reference not in ("pooled", "benign"):
        raise ValueError("reference must be 'pooled' or 'benign'")
    norm_n: list[FloatArray] = []
    norm_p: list[FloatArray] = []
    for sn, sp in zip(neg, pos, strict=True):
        an = np.asarray(sn, dtype=np.float64).ravel()
        ap = np.asarray(sp, dtype=np.float64).ravel()
        ref = an if reference == "benign" else np.r_[an, ap]
        norm_n.append(rank_normalize(an, ref))
        norm_p.append(rank_normalize(ap, ref))
    return combine(norm_n, how), combine(norm_p, how)
