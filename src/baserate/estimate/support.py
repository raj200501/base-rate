"""Where each interval method is statistically supported.

Experiment E0 measured the coverage of every interval method on a grid of
prevalences, review budgets, sample sizes, score distributions, and cluster
structures. Configurations where a method under-covered are listed in
``unsupported_cells.json`` (generated from the banked E0 artifact) and in
docs/UNSUPPORTED.md. :func:`check` matches a call against that list and emits
:class:`UnsupportedCellWarning`, because the score distribution of a real
monitor is unknown: a configuration is unsupported if the method failed for
any score distribution at the nearest grid point.
"""

from __future__ import annotations

import json
import math
import warnings
from functools import cache
from importlib import resources
from typing import Any

DATA_FILE = "unsupported_cells.json"
WEIGHTED = "weighted-design"
"""Pseudo cell id reported when sampling weights are used (not validated in E0)."""


class UnsupportedCellWarning(UserWarning):
    """The interval method under-covered in E0 for a configuration like this one."""


@cache
def _cells() -> tuple[dict[str, Any], ...]:
    text = resources.files("baserate.estimate").joinpath(DATA_FILE).read_text(encoding="utf-8")
    data = json.loads(text)
    return tuple(data.get("cells", []))


def _nearest(value: float, grid: list[float], log: bool = True) -> float:
    key = (lambda g: abs(math.log(g) - math.log(value))) if log else (lambda g: abs(g - value))
    return min(grid, key=key)


def check(
    method: str,
    prevalence: float,
    flag_rate: float,
    n_neg: int,
    n_pos: int,
    clustered: bool,
    emit: bool = True,
    metric: str = "budget_recall",
) -> tuple[str, ...]:
    """Return (and by default warn about) unsupported E0 cells matching a call.

    Args:
        method: Interval method name.
        prevalence: Deployment prevalence.
        flag_rate: Review flag rate.
        n_neg: Benign sample size.
        n_pos: Violation sample size.
        clustered: Whether traces share source runs.
        emit: Emit :class:`UnsupportedCellWarning` for each match.
        metric: E0 metric family of the call (``budget_recall`` for
            :func:`baserate.estimate.recall_at_budget`).

    Returns:
        Ids of the matching unsupported cells.
    """
    cells = [
        c
        for c in _cells()
        if c.get("estimator") == method and c.get("metric", "budget_recall") == metric
    ]
    if not cells:
        return ()

    def grid(key: str) -> list[float]:
        return sorted({float(c[key]) for c in cells})

    want = {
        "prevalence": _nearest(prevalence, grid("prevalence")),
        "flag_rate": _nearest(flag_rate, grid("flag_rate")),
        "n_neg": _nearest(n_neg, grid("n_neg")),
        "n_pos": _nearest(n_pos, grid("n_pos")),
    }
    hits = tuple(
        str(c["id"])
        for c in cells
        if all(math.isclose(float(c[k]), v) for k, v in want.items())
        and bool(c.get("clustered", False)) == clustered
    )
    if emit:
        for cell in hits:
            warnings.warn(
                f"{method} under-covered in E0 cell {cell}; see docs/UNSUPPORTED.md",
                UnsupportedCellWarning,
                stacklevel=3,
            )
    return hits
