"""Certify: how many independent runs a claim needs, and how many more an existing claim needs.

Runs offline in about a second, with no model calls:

    python examples/certify.py

Part 1 plans a study from design targets alone. Part 2 asks how far an
existing claim is from certified, using the fixture scores in
examples/fixtures/monitor_scores.csv (recorded Armour mock-tool traces;
demonstration data, not study results). The same answers come from

    baserate certify --fpr 0.001 --recall 0.9 --have-runs N --have-events K
"""

from __future__ import annotations

from pathlib import Path

from baserate.certify import (
    fpr_runs_for_power,
    joint_plan,
    more_runs_fpr,
    zero_fp_runs,
    zero_miss_runs,
)
from baserate.scores import read_scores

SCORES = Path(__file__).resolve().parent / "fixtures" / "monitor_scores.csv"
MAX_FPR = 1e-3  # claim: at most 1 false positive per 1,000 benign runs
MIN_RECALL = 0.9  # claim: at least 90% of violation runs caught
ALPHA = 0.05  # one-sided 95% confidence
POWER = 0.8  # wanted probability that the study certifies
THRESHOLD = 0.2  # the monitor's own decision threshold (flag when score >= 0.2)


def main() -> None:
    """Print a study plan, then the gap for the fixture's existing claim."""
    # Part 1. Planning. Every count is of independent source runs: procedural
    # variants of one run, or several traces from it, count once.
    print(f"Plan (design targets only, one-sided {1 - ALPHA:.0%}):")
    print(
        f"  FPR <= {MAX_FPR:g}: {zero_fp_runs(MAX_FPR, ALPHA):,} independent benign runs, "
        "none with a false positive"
    )
    print(
        f"  recall >= {MIN_RECALL:g}: {zero_miss_runs(MIN_RECALL, ALPHA):,} independent "
        "violation runs, all detected"
    )
    # A real monitor will have some false positives. If its true FPR is half
    # the target, this many runs certify with probability POWER or more.
    plan = fpr_runs_for_power(MAX_FPR, true_fpr=MAX_FPR / 2, power=POWER, alpha=ALPHA)
    print(
        f"  FPR <= {MAX_FPR:g} if the true FPR is {MAX_FPR / 2:g}: {plan.runs:,} benign runs "
        f"(up to {plan.events_allowed} false positives allowed) for power {POWER:g}"
    )
    joint = joint_plan(MIN_RECALL, MAX_FPR, ALPHA)
    print(
        f"  both claims at an overall {1 - ALPHA:.0%}: {joint.n_neg:,} benign and "
        f"{joint.n_pos:,} violation runs with no false positive and no miss\n"
    )

    # Part 2. An existing claim. A benign run counts as a false positive if any
    # of its traces is flagged, so count clusters, not traces.
    print("Existing claim (fixture data: recorded Armour mock-tool traces, not study results):")
    scores = read_scores(SCORES)
    flagged = scores.neg >= THRESHOLD
    fp_runs = len({c for c, f in zip(scores.neg_clusters, flagged, strict=True) if f})
    runs = scores.n_neg_clusters
    more = more_runs_fpr(fp_runs, runs, MAX_FPR, ALPHA)
    print(
        f"  {fp_runs} of {runs} independent benign runs have a false positive at score >= "
        f"{THRESHOLD:g}; one-sided upper bound on the FPR {more.current_bound:.3f}"
    )
    print(
        f"  {more.if_no_events:,} more independent benign runs with no false positive are "
        f"needed to claim a false-positive rate of at most {MAX_FPR:g} at {1 - ALPHA:.0%}"
    )
    if more.at_observed_rate is None:
        print("  if false positives continue at the observed rate, no number of runs is enough")
    else:
        print(f"  at the observed rate, about {more.at_observed_rate:,} more")


if __name__ == "__main__":
    main()
