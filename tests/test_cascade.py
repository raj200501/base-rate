"""Tests for baserate.cascade: two-stage cascades at a review budget, Pareto fronts, ensembles.

The hand-computed cases use four benign and two violation traces at
prevalence 0.2, small enough to reweight by hand: case-control reweighting
gives each benign trace deployment mass 0.8 / 4 = 0.2 and each violation
trace 0.2 / 2 = 0.1. Larger cases use synthetic scores (simulated, not
monitor results).
"""

from __future__ import annotations

import itertools
from typing import Any, cast

import numpy as np
import pytest

from baserate.cascade import (
    CascadePoint,
    TwoStageScores,
    best_under_budget,
    combine,
    default_escalation_rates,
    ensemble_scores,
    escalation_rule,
    evaluate_rule,
    pareto_front,
    rank_normalize,
    split_by_run,
    sweep_cascade,
    two_stage_scores,
    validate_holdout,
)
from baserate.cascade.optimize import PointKind

PI = 0.2  # prevalence of the hand case
C1, C2 = 1.0, 10.0


def hand_scores() -> TwoStageScores:
    """Benign stage-1 [0, 1, 2, 3] with stage-2 [0, 1, 5, 7]; violations [2, 3] with [9, 8]."""
    return two_stage_scores([0, 1, 2, 3], [0, 1, 5, 7], [2, 3], [9, 8])


def pt(cost: float, recall: float, kind: PointKind = "cascade") -> CascadePoint:
    """A point with only cost and recall set (for front and budget selection)."""
    return CascadePoint(kind, 0.0, 0.0, 0.0, cost, recall, 0.0, 0.0, 0.0)


def synthetic(
    rng: np.random.Generator, n_runs: int = 40, viol_runs: int = 20, per_run: int = 3
) -> TwoStageScores:
    """Synthetic clustered scores: ``per_run`` benign traces per run, one violation in each
    of the first ``viol_runs`` runs (matched by run label)."""
    neg_labels = np.repeat([f"r{i}" for i in range(n_runs)], per_run)
    pos_labels = np.array([f"r{i}" for i in range(viol_runs)])
    s1n = rng.normal(size=neg_labels.size)
    s2n = 0.5 * s1n + rng.normal(size=neg_labels.size)
    s1p = rng.normal(1.5, 1.0, size=viol_runs)
    s2p = rng.normal(2.5, 1.0, size=viol_runs)
    return two_stage_scores(s1n, s2n, s1p, s2p, neg_labels, pos_labels)


def reference(
    sc: TwoStageScores, prevalence: float, flag_rate: float, tau: float, tie: float
) -> tuple[float, float, float]:
    """Escalation rate, recall, and review rate by direct summation over traces.

    An independent re-derivation of the documented rule: case-control masses,
    randomized escalation at ``tau``, then the review budget spent from the
    highest stage-2 score down, randomized at the last level.
    """
    s1 = np.r_[sc.s1_neg, sc.s1_pos]
    s2 = np.r_[sc.s2_neg, sc.s2_pos]
    is_pos = np.r_[np.zeros(sc.n_neg, bool), np.ones(sc.n_pos, bool)]
    mass = np.r_[
        np.full(sc.n_neg, (1 - prevalence) / sc.n_neg), np.full(sc.n_pos, prevalence / sc.n_pos)
    ]
    esc = mass * np.where(s1 > tau, 1.0, np.where(s1 == tau, tie, 0.0))
    e = float(esc.sum())
    if e < flag_rate - 1e-12:  # every escalated trace is reviewed
        return e, float(esc[is_pos].sum()) / prevalence, e
    remaining, caught = flag_rate, 0.0
    for level in sorted(set(s2[esc > 0].tolist()), reverse=True):
        at = s2 == level
        take = min(1.0, remaining / float(esc[at].sum()))
        caught += take * float(esc[at & is_pos].sum())
        remaining -= take * float(esc[at].sum())
        if remaining <= 1e-15:
            break
    return e, caught / prevalence, flag_rate


# ------------------------------------------------------------------ scores bundle


def test_two_stage_scores_bundles_and_validates() -> None:
    sc = two_stage_scores([[0, 1]], [0.5, np.nan], [2], [3], ["a", "b"], ["a"])
    assert (sc.n_neg, sc.n_pos) == (2, 1)
    assert np.isnan(sc.s2_neg[1])  # stage 2 never scored this trace: allowed here
    assert sc.neg_clusters is not None and list(sc.neg_clusters) == ["a", "b"]
    cases = [
        ("at least one", lambda: two_stage_scores([], [], [1], [1])),
        ("align", lambda: two_stage_scores([0, 1], [0], [1], [1])),
        ("stage-1", lambda: two_stage_scores([np.nan], [0], [1], [1])),
        ("stage-1", lambda: two_stage_scores([0], [0], [np.inf], [1])),
        ("finite or NaN", lambda: two_stage_scores([0], [np.inf], [1], [1])),
        ("cluster label", lambda: two_stage_scores([0, 1], [0, 1], [1], [1], ["a"])),
        ("cluster label", lambda: two_stage_scores([0], [0], [1], [1], None, ["a", "b"])),
    ]
    for pattern, call in cases:
        with pytest.raises(ValueError, match=pattern):
            call()


def test_subset_keeps_alignment_and_labels() -> None:
    sc = two_stage_scores([0, 1, 2], [3, 4, 5], [6, 7], [8, 9], ["a", "b", "c"], ["x", "y"])
    sub = sc.subset([True, False, True], [False, True])
    np.testing.assert_array_equal(sub.s1_neg, [0, 2])
    np.testing.assert_array_equal(sub.s2_neg, [3, 5])
    np.testing.assert_array_equal(sub.s1_pos, [7])
    np.testing.assert_array_equal(sub.s2_pos, [9])
    assert sub.neg_clusters is not None and list(sub.neg_clusters) == ["a", "c"]
    assert sub.pos_clusters is not None and list(sub.pos_clusters) == ["y"]
    plain = two_stage_scores([0, 1], [0, 1], [2], [2]).subset([True, False], [True])
    assert plain.neg_clusters is None and plain.pos_clusters is None
    with pytest.raises(ValueError, match="at least one"):
        sc.subset([False, False, False], [True, True])


# ------------------------------------------------------------------ hand-computed cascades


def test_escalation_rule_reweights_the_case_control_sample() -> None:
    # stage-1 levels 3 and 2 each hold mass 0.2 + 0.1 = 0.3, so escalating 0.45
    # takes level 3 and half of level 2
    tau, tie = escalation_rule(hand_scores(), PI, 0.45)
    assert (tau, tie) == pytest.approx((2.0, 0.5))
    tau, tie = escalation_rule(hand_scores(), PI, 0.1)
    assert (tau, tie) == pytest.approx((3.0, 1 / 3))


@pytest.mark.parametrize(
    ("flag_rate", "recall", "precision", "fpr"),
    [
        # reviews 0.05 at stage-2 score 9 and half of the 0.1 at 8
        (0.1, 0.5, 1.0, 0.0),
        # both violations (0.15) and a quarter of the 0.2 benign mass at 7
        (0.2, 0.75, 0.75, 0.05 / 0.8),
        (0.3, 0.75, 0.5, 0.15 / 0.8),
    ],
)
def test_evaluate_rule_hand_values(
    flag_rate: float, recall: float, precision: float, fpr: float
) -> None:
    p = evaluate_rule(hand_scores(), PI, flag_rate, 2.0, 0.5, C1, C2)
    # escalated: benign 0.2 + 0.1, violations 0.1 + 0.05
    assert p.escalation_rate == pytest.approx(0.45)
    assert p.cost == pytest.approx(C1 + 0.45 * C2)
    assert p.cost == pytest.approx(5.5)
    assert p.recall == pytest.approx(recall)
    assert p.precision == pytest.approx(precision)
    assert p.fpr == pytest.approx(fpr)
    assert p.review_rate == pytest.approx(flag_rate)
    assert (p.kind, p.threshold, p.tie_fraction) == ("cascade", 2.0, 0.5)


def test_escalation_below_the_flag_rate_reviews_every_escalated_trace() -> None:
    # 0.45 escalates but 0.5 could be reviewed: the rest of the budget is unspent
    p = evaluate_rule(hand_scores(), PI, 0.5, 2.0, 0.5, C1, C2)
    assert p.review_rate == pytest.approx(0.45)
    assert p.recall == pytest.approx(0.75)  # P(escalated | violation)
    assert p.precision == pytest.approx(PI * 0.75 / 0.45)
    assert p.fpr == pytest.approx(0.375)  # P(escalated | benign)
    nothing = evaluate_rule(hand_scores(), PI, 0.1, 10.0, 1.0, C1, C2)
    assert (nothing.escalation_rate, nothing.recall, nothing.review_rate) == (0.0, 0.0, 0.0)
    assert (nothing.precision, nothing.fpr, nothing.cost) == (0.0, 0.0, C1)


def test_cascade_metrics_ignore_the_case_control_sample_sizes() -> None:
    sc = hand_scores()
    copies = two_stage_scores(
        np.tile(sc.s1_neg, 2), np.tile(sc.s2_neg, 2), np.tile(sc.s1_pos, 3), np.tile(sc.s2_pos, 3)
    )
    for flag_rate in (0.1, 0.2, 0.5):
        a = evaluate_rule(sc, PI, flag_rate, 2.0, 0.5, C1, C2)
        b = evaluate_rule(copies, PI, flag_rate, 2.0, 0.5, C1, C2)
        for name in ("escalation_rate", "cost", "recall", "precision", "fpr", "review_rate"):
            assert getattr(b, name) == pytest.approx(getattr(a, name))


def test_evaluate_rule_matches_direct_summation_on_tied_scores() -> None:
    rng = np.random.default_rng(3)
    for _ in range(25):
        sc = two_stage_scores(
            rng.integers(0, 6, 30),
            rng.integers(0, 8, 30),
            rng.integers(2, 8, 10),
            rng.integers(3, 10, 10),
        )
        prevalence = float(rng.choice([0.01, 0.05, 0.3]))
        flag_rate = float(rng.choice([0.005, 0.02, 0.1, 0.4]))
        tau = float(rng.integers(0, 8))
        tie = float(rng.uniform())
        p = evaluate_rule(sc, prevalence, flag_rate, tau, tie, C1, C2)
        e, recall, review = reference(sc, prevalence, flag_rate, tau, tie)
        assert p.escalation_rate == pytest.approx(e, abs=1e-12)
        assert p.recall == pytest.approx(recall, abs=1e-12)
        assert p.review_rate == pytest.approx(review, abs=1e-12)
        assert p.cost == pytest.approx(C1 + e * C2)


def test_evaluate_rule_rejects_bad_inputs() -> None:
    sc = hand_scores()
    with pytest.raises(ValueError, match="costs"):
        evaluate_rule(sc, PI, 0.1, 2.0, 0.5, -1.0, C2)
    with pytest.raises(ValueError, match="costs"):
        evaluate_rule(sc, PI, 0.1, 2.0, 0.5, C1, -0.5)
    with pytest.raises(ValueError, match="tie_fraction"):
        evaluate_rule(sc, PI, 0.1, 2.0, 1.5, C1, C2)
    with pytest.raises(ValueError, match="prevalence"):
        evaluate_rule(sc, 1.0, 0.1, 2.0, 0.5, C1, C2)
    with pytest.raises(ValueError, match="flag_rate"):
        evaluate_rule(sc, PI, 0.0, 2.0, 0.5, C1, C2)
    with pytest.raises(ValueError, match="flag_rate"):
        escalation_rule(sc, PI, 0.0)


# ------------------------------------------------------------------ missing stage-2 scores


def test_nan_stage2_scores_are_accepted_only_below_every_threshold_used() -> None:
    # stage 2 never scored the two benign traces with stage-1 scores 0 and 1
    sc = two_stage_scores([0, 1, 2, 3], [np.nan, np.nan, 5, 7], [2, 3], [9, 8])
    # rates up to 0.6 escalate stage-1 scores of 2 and above only
    sweep = sweep_cascade(sc, PI, 0.1, C1, C2, escalation_rates=[0.1, 0.45, 0.6])
    assert sweep.stage2 is None and len(sweep.points) == 4
    assert sweep.candidates[1].recall == pytest.approx(0.5)
    assert sweep.candidates[2].escalation_rate == pytest.approx(0.6)
    # a rate whose rule escalates an unscored trace is an error
    with pytest.raises(ValueError, match="no stage-2 score"):
        sweep_cascade(sc, PI, 0.1, C1, C2, escalation_rates=[0.45, 0.7])
    with pytest.raises(ValueError, match="no stage-2 score"):
        sweep_cascade(sc, PI, 0.1, C1, C2)  # the default grid ends at e = 1
    # at the threshold itself: allowed with tie fraction 0, rejected above 0
    at = evaluate_rule(sc, PI, 0.1, 1.0, 0.0, C1, C2)
    assert at.escalation_rate == pytest.approx(0.6)
    with pytest.raises(ValueError, match="1 escalated traces have no stage-2 score"):
        evaluate_rule(sc, PI, 0.1, 1.0, 0.25, C1, C2)
    # missing violation scores follow the same rule
    pos_nan = two_stage_scores([0, 1, 2, 3], [0, 1, 5, 7], [0.5, 3], [np.nan, 8])
    assert evaluate_rule(pos_nan, PI, 0.1, 2.0, 1.0, C1, C2).recall == pytest.approx(0.5)
    with pytest.raises(ValueError, match="no stage-2 score"):
        evaluate_rule(pos_nan, PI, 0.1, 0.0, 1.0, C1, C2)


def test_all_stage2_scores_missing_with_nothing_escalated() -> None:
    sc = two_stage_scores([0, 1], [np.nan, np.nan], [2], [np.nan])
    p = evaluate_rule(sc, PI, 0.1, 5.0, 1.0, C1, C2)
    assert (p.escalation_rate, p.recall, p.cost) == (0.0, 0.0, C1)


# ------------------------------------------------------------------ sweeps and baselines


def test_sweep_cascade_hand_case_with_baselines_and_front() -> None:
    sweep = sweep_cascade(hand_scores(), PI, 0.1, C1, C2, escalation_rates=[1.0, 0.45, 0.1, 0.45])
    assert [p.escalation_rate for p in sweep.candidates] == pytest.approx([0.1, 0.45, 1.0])
    for p in sweep.candidates:
        assert p.kind == "cascade"
        assert p.cost == pytest.approx(C1 + p.escalation_rate * C2)
    low, mid, full = sweep.candidates
    assert (mid.threshold, mid.tie_fraction) == pytest.approx((2.0, 0.5))
    assert (mid.recall, mid.precision, mid.fpr) == pytest.approx((0.5, 1.0, 0.0))
    # stage 1 alone: a third of stage-1 level 3 (mass 0.3) fills the budget
    s1 = sweep.stage1
    assert (s1.kind, s1.escalation_rate, s1.cost, s1.review_rate) == ("stage1", 0.0, C1, 0.1)
    assert (s1.threshold, s1.tie_fraction) == pytest.approx((3.0, 1 / 3))
    assert (s1.recall, s1.precision, s1.fpr) == pytest.approx((1 / 6, 1 / 3, 1 / 12))
    # stage 2 alone on all traffic: the violation at 9 fills the budget
    s2 = sweep.stage2
    assert s2 is not None
    assert (s2.kind, s2.escalation_rate, s2.cost) == ("stage2", 1.0, C2)
    assert (s2.threshold, s2.recall, s2.precision, s2.fpr) == pytest.approx((9.0, 0.5, 1.0, 0.0))
    # escalating exactly the flag rate reproduces stage 1; escalating all reproduces stage 2
    assert low.recall == pytest.approx(s1.recall)
    assert full.recall == pytest.approx(s2.recall)
    assert sweep.points == (*sweep.candidates, s1, s2)
    assert sweep.front == (s1, mid)
    assert sweep.best_under(6.0) == mid
    assert sweep.best_under(0.5) is None
    assert (sweep.prevalence, sweep.flag_rate, sweep.c1, sweep.c2) == (PI, 0.1, C1, C2)


def test_default_escalation_rates() -> None:
    rates = default_escalation_rates(0.002)
    assert rates.size == 25 and rates[0] == pytest.approx(0.002) and rates[-1] == 1.0
    ratios = rates[1:] / rates[:-1]
    np.testing.assert_allclose(ratios, ratios[0])
    np.testing.assert_allclose(default_escalation_rates(0.01, num=3), [0.01, 0.1, 1.0])
    np.testing.assert_array_equal(default_escalation_rates(1.0), [1.0])
    np.testing.assert_array_equal(default_escalation_rates(0.01, num=1), [1.0])
    with pytest.raises(ValueError, match="flag_rate"):
        default_escalation_rates(0.0)
    with pytest.raises(ValueError, match="flag_rate"):
        default_escalation_rates(1.5)
    with pytest.raises(ValueError, match="num"):
        default_escalation_rates(0.01, num=0)


def test_escalating_less_than_the_flag_rate_is_dominated_by_stage_one() -> None:
    sc = synthetic(np.random.default_rng(4))
    sweep = sweep_cascade(sc, 0.05, 0.1, C1, C2, escalation_rates=[0.02, 0.05, 0.08])
    for p in sweep.candidates:
        assert p.review_rate == pytest.approx(p.escalation_rate)
        assert p.precision == pytest.approx(0.05 * p.recall / p.escalation_rate)
        assert p.recall <= sweep.stage1.recall + 1e-12
        assert p.cost > sweep.stage1.cost
    assert all(p.kind != "cascade" for p in sweep.front)


@pytest.mark.parametrize("rates", [[], [0.0, 0.5], [0.5, 1.5], [-0.1]])
def test_sweep_rejects_bad_escalation_rates(rates: list[float]) -> None:
    with pytest.raises(ValueError, match="escalation rates"):
        sweep_cascade(hand_scores(), PI, 0.1, C1, C2, escalation_rates=rates)


def test_sweep_rejects_negative_costs() -> None:
    with pytest.raises(ValueError, match="costs"):
        sweep_cascade(hand_scores(), PI, 0.1, -1.0, C2)


# ------------------------------------------------------------------ Pareto front and budget


def test_pareto_front_on_a_constructed_set() -> None:
    a, b, c, d = pt(1.0, 0.2), pt(2.0, 0.5), pt(2.0, 0.4), pt(3.0, 0.5)
    e, f, g = pt(4.0, 0.9), pt(0.5, 0.1), pt(5.0, 0.85)
    h = pt(1.0, 0.2, "stage1")  # same cost and recall as a, listed later
    front = pareto_front([a, b, c, d, e, f, g, h])
    # c and d are dominated by b, g by e; of the duplicates a and h the first stays
    assert front == (f, a, b, e)
    assert front[1] is a
    assert pareto_front([]) == ()


def test_pareto_front_is_non_dominated_and_sorted() -> None:
    rng = np.random.default_rng(0)
    points = [
        pt(float(c), float(r) / 5)
        for c, r in zip(rng.integers(0, 6, 60), rng.integers(0, 6, 60), strict=True)
    ]
    front = pareto_front(points)
    costs = [p.cost for p in front]
    recalls = [p.recall for p in front]
    assert all(x < y for x, y in itertools.pairwise(costs))
    assert all(x < y for x, y in itertools.pairwise(recalls))

    def dominates(p: CascadePoint, q: CascadePoint) -> bool:
        return (
            p.cost <= q.cost and p.recall >= q.recall and (p.cost, p.recall) != (q.cost, q.recall)
        )

    for p in front:
        assert not any(dominates(q, p) for q in points)
    for q in points:
        if not any(q is p for p in front):
            assert any(dominates(p, q) or (p.cost, p.recall) == (q.cost, q.recall) for p in front)


def test_best_under_budget() -> None:
    points = [pt(1.0, 0.2), pt(2.0, 0.5), pt(3.0, 0.5), pt(4.0, 0.9)]
    assert best_under_budget(points, 3.5) is points[1]  # equal recall: the cheaper point
    assert best_under_budget(points, 4.0) is points[3]
    assert best_under_budget(points, 1.0 - 1e-13) is points[0]  # within the tolerance
    assert best_under_budget(points, 0.5) is None
    assert best_under_budget([], 10.0) is None


# ------------------------------------------------------------------ train and test splits


def run_labels(sc: TwoStageScores) -> set[str]:
    assert sc.neg_clusters is not None and sc.pos_clusters is not None
    return {str(x) for x in sc.neg_clusters} | {str(x) for x in sc.pos_clusters}


def test_split_by_run_keeps_every_source_run_on_one_side() -> None:
    sc = synthetic(np.random.default_rng(1))
    train, test = split_by_run(sc, 0.3, seed=5)
    assert run_labels(train).isdisjoint(run_labels(test))
    assert run_labels(train) | run_labels(test) == run_labels(sc)
    assert (train.n_neg + test.n_neg, train.n_pos + test.n_pos) == (sc.n_neg, sc.n_pos)
    # stratified: runs with a violation and benign-only runs are each split at 30%
    assert test.pos_clusters is not None
    assert len(set(test.pos_clusters.tolist())) == round(0.3 * 20)
    viol_runs = {f"r{i}" for i in range(20)}
    assert len(run_labels(test) - viol_runs) == round(0.3 * 20)
    # every trace keeps its own scores and label
    assert sc.neg_clusters is not None and train.neg_clusters is not None
    origin = dict(
        zip(
            sc.s1_neg.tolist(),
            zip(sc.s2_neg.tolist(), sc.neg_clusters.tolist(), strict=True),
            strict=True,
        )
    )
    for part in (train, test):
        assert part.neg_clusters is not None
        for s1, s2, label in zip(
            part.s1_neg.tolist(), part.s2_neg.tolist(), part.neg_clusters.tolist(), strict=True
        ):
            assert origin[s1] == (s2, label)


def test_split_by_run_is_deterministic_given_the_seed() -> None:
    sc = synthetic(np.random.default_rng(1))
    first = split_by_run(sc, 0.5, seed=3)
    again = split_by_run(sc, 0.5, seed=3)
    other = split_by_run(sc, 0.5, seed=4)
    for x, y in zip(first, again, strict=True):
        np.testing.assert_array_equal(x.s1_neg, y.s1_neg)
        np.testing.assert_array_equal(x.s2_pos, y.s2_pos)
        assert run_labels(x) == run_labels(y)
    assert run_labels(first[1]) != run_labels(other[1])


def test_split_by_run_without_labels_treats_each_trace_as_a_run() -> None:
    rng = np.random.default_rng(2)
    sc = two_stage_scores(
        rng.normal(size=20), rng.normal(size=20), rng.normal(size=10), rng.normal(size=10)
    )
    train, test = split_by_run(sc, 0.25, seed=0)
    assert (test.n_neg, test.n_pos) == (5, round(0.25 * 10))
    assert (train.n_neg, train.n_pos) == (15, 10 - round(0.25 * 10))
    assert train.neg_clusters is None and test.pos_clusters is None


def test_split_by_run_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="test_fraction"):
        split_by_run(hand_scores(), 0.0)
    with pytest.raises(ValueError, match="test_fraction"):
        split_by_run(hand_scores(), 1.0)
    with pytest.raises(ValueError, match="too few"):
        split_by_run(two_stage_scores([0.0], [0.0], [1.0], [1.0]), 0.5)
    one_run = two_stage_scores([0, 1], [0, 1], [2], [2], ["r", "r"], ["r"])
    with pytest.raises(ValueError, match="too few"):
        split_by_run(one_run, 0.5)


# ------------------------------------------------------------------ held-out validation


@pytest.fixture(scope="module")
def halves() -> tuple[TwoStageScores, TwoStageScores]:
    return split_by_run(synthetic(np.random.default_rng(11), 120, 60, 2), 0.5, seed=1)


def test_validate_holdout_interval_brackets_its_estimate(
    halves: tuple[TwoStageScores, TwoStageScores],
) -> None:
    train, test = halves
    res = validate_holdout(train, test, 0.05, 0.05, C1, C2, max_cost=4.0, reps=60, seed=2)
    assert res.recall.lower <= res.recall.estimate <= res.recall.upper
    assert res.cost.lower <= res.cost.estimate <= res.cost.upper
    assert res.recall.lower < res.recall.upper  # resampling 30 test violation runs varies recall
    assert (res.recall.method, res.recall.alpha) == ("holdout_cluster_bootstrap", 0.05)
    # the chosen rule: highest training recall within budget, ties to lower escalation
    sweep = sweep_cascade(train, 0.05, 0.05, C1, C2)
    feasible = [p for p in sweep.candidates if p.cost <= 4.0 + 1e-12]
    top = max(p.recall for p in feasible)
    assert res.chosen == next(p for p in feasible if p.recall >= top - 1e-12)
    # the same rule, unchanged, on the test split
    want = evaluate_rule(test, 0.05, 0.05, res.chosen.threshold, res.chosen.tie_fraction, C1, C2)
    assert res.test == want
    assert (res.recall.estimate, res.cost.estimate) == (want.recall, want.cost)
    assert (res.n_train_neg, res.n_train_pos) == (train.n_neg, train.n_pos)
    assert (res.n_test_neg, res.n_test_pos) == (test.n_neg, test.n_pos)
    assert train.pos_clusters is not None and test.neg_clusters is not None
    assert res.n_train_pos_runs == len(set(train.pos_clusters.tolist()))
    assert res.n_test_neg_runs == len(set(test.neg_clusters.tolist()))
    assert res.n_train_neg_runs + res.n_test_neg_runs == 120
    assert (res.reps, res.max_cost) == (60, 4.0)
    again = validate_holdout(train, test, 0.05, 0.05, C1, C2, max_cost=4.0, reps=60, seed=2)
    assert again == res


def test_validate_holdout_bayesian_bootstrap_and_a_single_replicate(
    halves: tuple[TwoStageScores, TwoStageScores],
) -> None:
    train, test = halves
    res = validate_holdout(
        train,
        test,
        0.05,
        0.05,
        C1,
        C2,
        4.0,
        escalation_rates=[0.1, 0.2, 0.3],
        reps=1,
        kind="bayesian",
    )
    assert (res.recall.method, res.reps) == ("holdout_bayesian_bootstrap", 1)
    assert res.recall.lower <= res.recall.estimate <= res.recall.upper
    assert res.chosen in sweep_cascade(train, 0.05, 0.05, C1, C2, [0.1, 0.2, 0.3]).candidates


def test_validate_holdout_selects_what_the_point_selection_selects_at_the_budget_edge() -> None:
    # max_cost equals the realized cost c1 + e * c2 of the only candidate; with a
    # large c2 the nominal c1 + rate * c2 can round to the other side of max_cost,
    # which used to leave the bootstrap with no candidate at all
    sc = hand_scores()
    for c2 in (1e3, 1e6, 1e9):
        cand = sweep_cascade(sc, PI, 0.1, 0.0, c2, escalation_rates=[0.45]).candidates[0]
        res = validate_holdout(sc, sc, PI, 0.1, 0.0, c2, cand.cost, escalation_rates=[0.45], reps=5)
        assert res.chosen == cand


def test_validate_holdout_rejects_bad_inputs(halves: tuple[TwoStageScores, TwoStageScores]) -> None:
    train, test = halves
    with pytest.raises(ValueError, match="reps"):
        validate_holdout(train, test, 0.05, 0.05, C1, C2, 4.0, reps=0)
    with pytest.raises(ValueError, match="max_cost"):
        validate_holdout(train, test, 0.05, 0.05, C1, C2, 0.5)  # stage 1 alone costs 1


# ------------------------------------------------------------------ ensembles


def test_rank_normalize_mid_ranks_against_a_reference_sample() -> None:
    ref = [1.0, 2.0, 2.0, 3.0]
    got = rank_normalize([0.0, 1.0, 2.0, 2.5, 3.0, 4.0], ref)
    np.testing.assert_allclose(got, [0.0, 0.125, 0.5, 0.75, 0.875, 1.0])
    assert rank_normalize(np.array([[1.0, 3.0]]), ref).shape == (1, 2)
    assert float(rank_normalize(2.0, ref)) == 0.5
    np.testing.assert_array_equal(rank_normalize([-np.inf, np.inf], ref), [0.0, 1.0])
    # held-out scores against a training reference
    np.testing.assert_allclose(
        rank_normalize([-1.0, 4.5, 9.0, 20.0], np.arange(10.0)), [0.0, 0.5, 0.95, 1.0]
    )


def test_rank_normalize_keeps_missing_scores_missing() -> None:
    # a NaN score used to land above every reference value (rank 1)
    got = rank_normalize([np.nan, 2.0], [1.0, 2.0, 2.0, 3.0])
    assert np.isnan(got[0]) and got[1] == 0.5


@pytest.mark.parametrize("bad", [[], [1.0, np.nan], [0.0, np.inf]])
def test_rank_normalize_rejects_bad_references(bad: list[float]) -> None:
    with pytest.raises(ValueError, match="reference"):
        rank_normalize([1.0], bad)


def test_combine_mean_and_max() -> None:
    a, b = [0.1, 0.9, 0.5], [0.5, 0.3, 0.5]
    np.testing.assert_allclose(combine([a, b]), [0.3, 0.6, 0.5])
    np.testing.assert_allclose(combine([a, b], "max"), [0.5, 0.9, 0.5])
    np.testing.assert_allclose(combine(np.array([a, b])), [0.3, 0.6, 0.5])  # one row per monitor
    np.testing.assert_allclose(combine([a]), a)
    with pytest.raises(ValueError, match="at least one"):
        combine([])
    with pytest.raises(ValueError, match="how"):
        combine([a, b], cast(Any, "median"))
    with pytest.raises(ValueError):
        combine([a, [0.1]])  # monitors must score the same traces


def test_ensemble_scores_pooled_and_benign_references() -> None:
    neg = [np.array([0.0, 1.0, 2.0]), np.array([10.0, 30.0, 20.0])]
    pos = [np.array([3.0, 5.0]), np.array([40.0, 25.0])]
    en, ep = ensemble_scores(neg, pos, "mean")
    # pooled reference per monitor: [0, 1, 2, 3, 5] and [10, 30, 20, 40, 25]
    np.testing.assert_allclose(en, [(0.1 + 0.1) / 2, (0.3 + 0.7) / 2, (0.5 + 0.3) / 2])
    np.testing.assert_allclose(ep, [(0.7 + 0.9) / 2, (0.9 + 0.5) / 2])
    en, ep = ensemble_scores(neg, pos, "max", reference="benign")
    # benign reference: every violation score above the largest benign one maps to 1
    np.testing.assert_allclose(ep, [1.0, 1.0])
    np.testing.assert_allclose(en, [max(1 / 6, 1 / 6), max(1 / 2, 5 / 6), max(5 / 6, 1 / 2)])
    # pooled ranks keep distinct scores of one monitor apart; the benign reference merges the tail
    pooled_pos = ensemble_scores([neg[0]], [pos[0]])[1]
    benign_pos = ensemble_scores([neg[0]], [pos[0]], reference="benign")[1]
    assert pooled_pos[0] < pooled_pos[1] and benign_pos[0] == benign_pos[1]


def test_ensemble_scores_rejects_bad_inputs() -> None:
    one = [np.array([0.0, 1.0])]
    with pytest.raises(ValueError, match="same positive number"):
        ensemble_scores(one, [])
    with pytest.raises(ValueError, match="same positive number"):
        ensemble_scores([], [])
    with pytest.raises(ValueError, match="reference"):
        ensemble_scores(one, one, reference=cast(Any, "violations"))
