"""Prevalence-aware estimators for monitor evaluation.

Main entry points:

- :func:`recall_at_budget`: recall, precision, and FPR when a fixed fraction of
  traffic is reviewed, at a deployment prevalence, with intervals.
- :func:`fixed_threshold`: the same metrics at a monitor's own decision
  threshold, with cluster-aware exact bounds.
- :mod:`baserate.estimate.intervals`: exact, Wilson, Jeffreys, and Beta
  posterior intervals, and the zero-event upper bound.
"""

from baserate.estimate.atbudget import (
    BUDGET_METHODS,
    BudgetEstimate,
    Interval,
    recall_at_budget,
)
from baserate.estimate.fixed import FIXED_METHODS, FixedEstimate, fixed_threshold, rate_interval
from baserate.estimate.operating import (
    OperatingPoint,
    budget_point,
    flag_rate_from_budget,
)
from baserate.estimate.support import UnsupportedCellWarning

__all__ = [
    "BUDGET_METHODS",
    "FIXED_METHODS",
    "BudgetEstimate",
    "FixedEstimate",
    "Interval",
    "OperatingPoint",
    "UnsupportedCellWarning",
    "budget_point",
    "fixed_threshold",
    "flag_rate_from_budget",
    "rate_interval",
    "recall_at_budget",
]
