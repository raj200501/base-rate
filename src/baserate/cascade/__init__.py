"""Cost-recall optimization of cheap-to-expensive monitor cascades (RQ4).

Cascades (:mod:`baserate.cascade.optimize`):

- :func:`two_stage_scores`: bundle and validate stage-1 and stage-2 scores.
- :func:`sweep_cascade`: cost, recall, precision, and escalation rate over a
  grid of escalation rates, with single-monitor baselines and the Pareto front.
- :func:`best_under_budget` and :func:`pareto_front`: selection helpers.
- :func:`evaluate_rule`: a fixed escalation rule on new data.
- :func:`split_by_run` and :func:`validate_holdout`: choose on a training
  split, evaluate on a test split, with a cluster bootstrap interval.

Ensembles (:mod:`baserate.cascade.ensemble`), a separate concern:

- :func:`rank_normalize`, :func:`combine`, :func:`ensemble_scores`.
"""

from baserate.cascade.ensemble import combine, ensemble_scores, rank_normalize
from baserate.cascade.optimize import (
    CascadePoint,
    CascadeSweep,
    HoldoutResult,
    TwoStageScores,
    best_under_budget,
    default_escalation_rates,
    escalation_rule,
    evaluate_rule,
    pareto_front,
    split_by_run,
    sweep_cascade,
    two_stage_scores,
    validate_holdout,
)

__all__ = [
    "CascadePoint",
    "CascadeSweep",
    "HoldoutResult",
    "TwoStageScores",
    "best_under_budget",
    "combine",
    "default_escalation_rates",
    "ensemble_scores",
    "escalation_rule",
    "evaluate_rule",
    "pareto_front",
    "rank_normalize",
    "split_by_run",
    "sweep_cascade",
    "two_stage_scores",
    "validate_holdout",
]
