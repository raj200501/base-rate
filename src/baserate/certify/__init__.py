"""Sample sizes for certifying monitor recall and false-positive rate (RQ2).

Exact and closed-form calculators (:mod:`baserate.certify.samplesize`):

- :func:`zero_fp_runs`, :func:`zero_miss_runs`: zero-event sample sizes.
- :func:`events_allowed`, :func:`fpr_power`, :func:`recall_power`: what a
  given number of runs can certify, and with what probability.
- :func:`fpr_runs_for_power`, :func:`recall_runs_for_power`: runs needed for
  a target power (exact binomial search with a stays-above rule).
- :func:`joint_plan`, :func:`split_alpha`: recall and FPR together at one
  overall confidence.
- :func:`more_runs_fpr`, :func:`more_runs_recall`: how many more runs an
  existing claim needs.

Simulation-based planning at a review budget (:mod:`baserate.certify.atbudget`):

- :func:`budget_power`, :func:`plan_budget`.

Every count is of independent source runs; see :data:`CLUSTER_NOTE`.
"""

from baserate.certify.atbudget import BudgetDesign, BudgetPlan, budget_power, plan_budget
from baserate.certify.samplesize import (
    CLUSTER_NOTE,
    JointPlan,
    MoreRuns,
    RunPlan,
    events_allowed,
    fpr_power,
    fpr_runs_for_power,
    joint_plan,
    more_runs_fpr,
    more_runs_recall,
    recall_power,
    recall_runs_for_power,
    split_alpha,
    zero_fp_runs,
    zero_miss_runs,
)

__all__ = [
    "CLUSTER_NOTE",
    "BudgetDesign",
    "BudgetPlan",
    "JointPlan",
    "MoreRuns",
    "RunPlan",
    "budget_power",
    "events_allowed",
    "fpr_power",
    "fpr_runs_for_power",
    "joint_plan",
    "more_runs_fpr",
    "more_runs_recall",
    "plan_budget",
    "recall_power",
    "recall_runs_for_power",
    "split_alpha",
    "zero_fp_runs",
    "zero_miss_runs",
]
