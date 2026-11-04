"""Build a cascade: a cheap monitor screens all traffic and escalates a share to a costly one.

Runs offline in about a second, with no model calls:

    python examples/build_cascade.py

The input, examples/fixtures/cascade_scores.csv, holds recorded Armour
mock-tool traces scored by two Armour rule packs: the baseline pack as stage 1
(column score) and the reviewer-calibrated pack as stage 2 (column
stage2_score). It is demonstration data, not study results, and the costs
below are illustrative units. The same sweep comes from

    baserate cascade examples/fixtures/cascade_scores.csv \\
        --prevalence 1e-3 --budget 20 --c1 1 --c2 10 --max-cost 3
"""

from __future__ import annotations

from pathlib import Path

from baserate.cascade import sweep_cascade
from baserate.estimate import flag_rate_from_budget
from baserate.scores import read_scores

SCORES = Path(__file__).resolve().parent / "fixtures" / "cascade_scores.csv"
PREVALENCE = 1e-3  # assumed share of violations in deployment traffic
BUDGET = 20  # humans review 20 of every 10,000 trajectories
C1 = 1.0  # stage-1 cost per trajectory (illustrative units)
C2 = 10.0  # stage-2 cost per escalated trajectory (illustrative units)
MAX_COST = 3.0  # compute budget per trajectory


def main() -> None:
    """Sweep escalation rates, print the Pareto front and the best cascade under MAX_COST."""
    print("Demonstration data: recorded Armour mock-tool traces, not study results.\n")

    # 1. Read stage-1 and stage-2 scores; a cascade needs both columns.
    scores = read_scores(SCORES).two_stage()

    # 2. Escalate the top share e of traffic by stage-1 score to stage 2, for e
    #    on a grid from the flag rate to 1, and review the top 20 per 10,000 by
    #    stage-2 score among the escalated. Expected cost is C1 + e * C2.
    sweep = sweep_cascade(scores, PREVALENCE, flag_rate_from_budget(BUDGET), C1, C2)

    # 3. The Pareto front: no other point costs less and recalls more.
    print(f"Pareto front at prevalence {PREVALENCE:g}, budget {BUDGET}/10k:")
    print("  kind     escalation  cost    recall  precision")
    for pt in sweep.front:
        print(
            f"  {pt.kind:<8} {pt.escalation_rate:<11.4f} {pt.cost:<7.3f} "
            f"{pt.recall:<7.3f} {pt.precision:.3f}"
        )

    # 4. The best point within the compute budget. It is chosen on the same
    #    traces it is scored on, so it is optimistic; on real data, choose it on
    #    one split of source runs and test it on another
    #    (baserate.cascade.split_by_run and validate_holdout).
    best = sweep.best_under(MAX_COST)
    if best is None:
        print(f"\nNo cascade costs at most {MAX_COST:g} per trajectory.")
    else:
        print(
            f"\nBest under cost {MAX_COST:g}: {best.kind} escalating {best.escalation_rate:.4f} "
            f"of traffic, cost {best.cost:.3f}, recall {best.recall:.3f}, "
            f"precision {best.precision:.3f} (in-sample point estimates, no intervals)"
        )


if __name__ == "__main__":
    main()
