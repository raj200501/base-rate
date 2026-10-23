"""Binomial intervals for rare events.

All functions accept scalars or arrays (broadcast together) and return float
arrays. ``k`` is the number of events, ``n`` the number of trials, and
``alpha`` the total miss probability of a two-sided interval (0.05 for 95%).
One-sided bounds use the full ``alpha`` on their side.

- :func:`clopper_pearson`: the exact interval; never under-covers for iid
  binomial data.
- :func:`wilson`: the score interval; close to nominal on average, can
  under-cover near 0 and 1.
- :func:`jeffreys`: the equal-tailed Bayesian interval under a Beta(1/2, 1/2)
  prior, with the usual boundary fix (0 when k = 0, 1 when k = n).
- :func:`beta_posterior`: an equal-tailed credible interval for any Beta prior.
- :func:`zero_event_upper`: the exact one-sided upper bound when no events
  were seen, ``1 - alpha**(1/n)``; about ``3/n`` at 95% (the rule of three).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy import stats

FloatArray = NDArray[np.float64]
Side = Literal["two", "lower", "upper"]


def _prep(k: ArrayLike, n: ArrayLike) -> tuple[FloatArray, FloatArray]:
    k_arr = np.asarray(k, dtype=np.float64)
    n_arr = np.asarray(n, dtype=np.float64)
    if np.any(n_arr <= 0):
        raise ValueError("n must be positive")
    if np.any(k_arr < 0) or np.any(k_arr > n_arr):
        raise ValueError("k must satisfy 0 <= k <= n")
    k_b, n_b = np.broadcast_arrays(k_arr, n_arr)
    return np.asarray(k_b, dtype=np.float64), np.asarray(n_b, dtype=np.float64)


def _check_alpha(alpha: float) -> None:
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")


def clopper_pearson(
    k: ArrayLike, n: ArrayLike, alpha: float = 0.05, side: Side = "two"
) -> tuple[FloatArray, FloatArray]:
    """Exact (Clopper-Pearson) interval for a binomial proportion.

    Args:
        k: Events.
        n: Trials.
        alpha: Miss probability: split across both tails when ``side`` is
            ``"two"``, all on one tail otherwise.
        side: ``"two"`` for a two-sided interval, ``"lower"`` for a lower
            bound (upper end 1), ``"upper"`` for an upper bound (lower end 0).

    Returns:
        (lower, upper) arrays.
    """
    _check_alpha(alpha)
    k_arr, n_arr = _prep(k, n)
    tail = alpha / 2 if side == "two" else alpha
    with np.errstate(invalid="ignore"):
        lo = np.where(k_arr > 0, stats.beta.ppf(tail, k_arr, n_arr - k_arr + 1), 0.0)
        hi = np.where(k_arr < n_arr, stats.beta.ppf(1 - tail, k_arr + 1, n_arr - k_arr), 1.0)
    if side == "lower":
        hi = np.ones_like(hi)
    elif side == "upper":
        lo = np.zeros_like(lo)
    return np.asarray(lo, dtype=np.float64), np.asarray(hi, dtype=np.float64)


def wilson(k: ArrayLike, n: ArrayLike, alpha: float = 0.05) -> tuple[FloatArray, FloatArray]:
    """Two-sided Wilson score interval.

    Args:
        k: Events.
        n: Trials.
        alpha: Total miss probability.

    Returns:
        (lower, upper) arrays.
    """
    _check_alpha(alpha)
    k_arr, n_arr = _prep(k, n)
    z = float(stats.norm.ppf(1 - alpha / 2))
    p = k_arr / n_arr
    denom = 1 + z**2 / n_arr
    centre = (p + z**2 / (2 * n_arr)) / denom
    half = z * np.sqrt(p * (1 - p) / n_arr + z**2 / (4 * n_arr**2)) / denom
    return np.clip(centre - half, 0.0, 1.0), np.clip(centre + half, 0.0, 1.0)


def beta_posterior(
    k: ArrayLike, n: ArrayLike, alpha: float = 0.05, prior: tuple[float, float] = (1.0, 1.0)
) -> tuple[FloatArray, FloatArray]:
    """Equal-tailed credible interval of the Beta posterior.

    Args:
        k: Events.
        n: Trials.
        alpha: Total posterior mass outside the interval.
        prior: Beta prior parameters (a, b).

    Returns:
        (lower, upper) arrays.
    """
    _check_alpha(alpha)
    k_arr, n_arr = _prep(k, n)
    a, b = prior
    if a <= 0 or b <= 0:
        raise ValueError("prior parameters must be positive")
    lo = stats.beta.ppf(alpha / 2, k_arr + a, n_arr - k_arr + b)
    hi = stats.beta.ppf(1 - alpha / 2, k_arr + a, n_arr - k_arr + b)
    return np.asarray(lo, dtype=np.float64), np.asarray(hi, dtype=np.float64)


def jeffreys(k: ArrayLike, n: ArrayLike, alpha: float = 0.05) -> tuple[FloatArray, FloatArray]:
    """Jeffreys interval (Beta(1/2, 1/2) prior) with the boundary modification.

    Args:
        k: Events.
        n: Trials.
        alpha: Total miss probability.

    Returns:
        (lower, upper) arrays; the lower end is 0 when k = 0 and the upper
        end is 1 when k = n (Brown, Cai, and DasGupta 2001).
    """
    lo, hi = beta_posterior(k, n, alpha, prior=(0.5, 0.5))
    k_arr, n_arr = _prep(k, n)
    return np.where(k_arr == 0, 0.0, lo), np.where(k_arr == n_arr, 1.0, hi)


def zero_event_upper(n: ArrayLike, alpha: float = 0.05) -> FloatArray:
    """Exact one-sided upper bound on a rate when 0 events occurred in ``n`` trials.

    Args:
        n: Independent trials with no events.
        alpha: Miss probability of the one-sided bound.

    Returns:
        ``1 - alpha**(1/n)``, about ``3/n`` for alpha = 0.05.
    """
    _check_alpha(alpha)
    n_arr = np.asarray(n, dtype=np.float64)
    if np.any(n_arr <= 0):
        raise ValueError("n must be positive")
    return np.asarray(1.0 - alpha ** (1.0 / n_arr), dtype=np.float64)


IntervalFn = Callable[[ArrayLike, ArrayLike, float], tuple[FloatArray, FloatArray]]

INTERVALS: dict[str, IntervalFn] = {
    "clopper_pearson": clopper_pearson,
    "wilson": wilson,
    "jeffreys": jeffreys,
}
"""Two-sided binomial intervals by name."""
