"""Tests for baserate.certify: exact sample sizes, power, joint plans, more runs, budget planning.

Reference values come from brute force over every event count with the exact
one-sided Clopper-Pearson bound of baserate.estimate.intervals, which is the
decision rule the calculators document. Budget-mode tests use a synthetic
binormal pilot (simulated scores, not monitor results).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable
from typing import Literal

import numpy as np
import pytest
from numpy.typing import NDArray
from scipy import stats

from baserate.certify import (
    CLUSTER_NOTE,
    BudgetDesign,
    BudgetPlan,
    RunPlan,
    atbudget,
    budget_power,
    events_allowed,
    fpr_power,
    fpr_runs_for_power,
    joint_plan,
    more_runs_fpr,
    more_runs_recall,
    plan_budget,
    recall_power,
    recall_runs_for_power,
    split_alpha,
    zero_fp_runs,
    zero_miss_runs,
)
from baserate.estimate import budget_point, intervals

Metric = Literal["fpr", "recall"]
FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]

ALPHA = 0.05


# ------------------------------------------------------------------ reference helpers


def certified(metric: Metric, events: float, runs: int, bound: float, alpha: float) -> bool:
    """Reference decision: the one-sided exact bound meets the target.

    ``events`` counts false positives for ``fpr`` and misses for ``recall``.
    """
    if metric == "fpr":
        return bool(intervals.clopper_pearson(events, runs, alpha, side="upper")[1] <= bound)
    return bool(intervals.clopper_pearson(runs - events, runs, alpha, side="lower")[0] >= bound)


def certifying_counts(runs: int, bound: float, metric: Metric, alpha: float) -> NDArray[np.bool_]:
    """Whether each event count 0..runs certifies, evaluated for all counts at once."""
    k = np.arange(runs + 1, dtype=np.float64)
    if metric == "fpr":
        ok = intervals.clopper_pearson(k, runs, alpha, side="upper")[1] <= bound
    else:
        ok = intervals.clopper_pearson(runs - k, runs, alpha, side="lower")[0] >= bound
    return np.asarray(ok, dtype=np.bool_)


def brute_allowed(runs: int, bound: float, metric: Metric, alpha: float = ALPHA) -> int:
    """Most events among ``runs`` runs that still certify (-1 if none)."""
    hits = np.flatnonzero(certifying_counts(runs, bound, metric, alpha))
    return int(hits.max()) if hits.size else -1


def brute_power(runs: int, bound: float, p_event: float, metric: Metric, alpha: float) -> float:
    """Probability of certifying: binomial mass of the certifying event counts."""
    ok = certifying_counts(runs, bound, metric, alpha)
    k = np.arange(runs + 1)
    return float(stats.binom.pmf(k[ok], runs, p_event).sum())


def power_curve(
    metric: Metric, runs: IntArray, bound: float, assumed: float, alpha: float
) -> FloatArray:
    """The module's exact power at each run count (checked against brute force below)."""
    if metric == "fpr":
        return fpr_power(runs, bound, assumed, alpha)
    return recall_power(runs, bound, assumed, alpha)


def future_power(
    metric: Metric, events: int, runs: int, bound: float, extra: IntArray, alpha: float = ALPHA
) -> FloatArray:
    """P(certify) after ``extra`` more runs whose events are Binomial at the observed rate."""
    room = events_allowed(runs + extra, bound, metric, alpha) - events
    return np.asarray(stats.binom.cdf(room, extra, events / runs), dtype=np.float64)


def brute_future_power(metric: Metric, events: int, runs: int, bound: float, extra: int) -> float:
    """Brute-force version of :func:`future_power` for one ``extra``."""
    new = np.arange(extra + 1)
    ok = np.array([certified(metric, events + j, runs + extra, bound, ALPHA) for j in new])
    return float(stats.binom.pmf(new[ok], extra, events / runs).sum())


# ------------------------------------------------------------------ zero-event sizes


@pytest.mark.parametrize(
    ("max_fpr", "alpha", "expected"),
    [
        (0.1, 0.05, 29),
        (0.01, 0.05, 299),
        (0.001, 0.05, 2995),
        (1e-4, 0.05, 29956),
        (0.01, 0.01, 459),
    ],
)
def test_zero_fp_runs_hand_values(max_fpr: float, alpha: float, expected: int) -> None:
    n = zero_fp_runs(max_fpr, alpha)
    assert n == expected
    assert n == math.ceil(math.log(alpha) / math.log(1 - max_fpr))


def test_zero_fp_runs_is_about_three_over_f_at_95_percent() -> None:
    for f in (0.01, 0.001, 1e-4):
        assert zero_fp_runs(f) == pytest.approx(3 / f, rel=0.01)


@pytest.mark.parametrize("max_fpr", [0.2, 0.05, 0.01, 0.003])
@pytest.mark.parametrize("alpha", [0.1, 0.05, 0.01])
def test_zero_fp_runs_is_the_smallest_certifying_n(max_fpr: float, alpha: float) -> None:
    n = zero_fp_runs(max_fpr, alpha)
    # zero false positives in n runs: the exact upper bound is 1 - alpha ** (1 / n)
    assert (
        intervals.zero_event_upper(n, alpha) <= max_fpr < intervals.zero_event_upper(n - 1, alpha)
    )
    assert certified("fpr", 0, n, max_fpr, alpha)
    assert not certified("fpr", 0, n - 1, max_fpr, alpha)


@pytest.mark.parametrize(
    ("min_recall", "alpha", "expected"),
    [
        (0.5, 0.05, 5),
        (0.8, 0.05, 14),
        (0.9, 0.05, 29),
        (0.95, 0.05, 59),
        (0.99, 0.05, 299),
        (0.9, 0.01, 44),
    ],
)
def test_zero_miss_runs_hand_values(min_recall: float, alpha: float, expected: int) -> None:
    n = zero_miss_runs(min_recall, alpha)
    assert n == expected
    assert n == math.ceil(math.log(alpha) / math.log(min_recall))
    # all n detected: the exact lower bound is alpha ** (1 / n)
    assert alpha ** (1 / n) >= min_recall > alpha ** (1 / (n - 1))
    assert certified("recall", 0, n, min_recall, alpha)
    assert not certified("recall", 0, n - 1, min_recall, alpha)


@pytest.mark.parametrize("metric", ["fpr", "recall"])
def test_zero_event_sizes_follow_the_decision_rule_at_exact_boundaries(metric: Metric) -> None:
    # alpha = q ** n puts the closed form exactly on an integer, where rounding
    # can move it by one; the answer must still be the smallest certifying n
    fn = zero_fp_runs if metric == "fpr" else zero_miss_runs
    for q in (0.75, 0.6, 0.5):
        for n in range(1, 31):
            bound = 1 - q if metric == "fpr" else q
            got = fn(bound, q**n)
            assert abs(got - n) <= 1
            assert certified(metric, 0, got, bound, q**n)
            assert got == 1 or not certified(metric, 0, got - 1, bound, q**n)


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5])
def test_zero_event_sizes_reject_bad_targets(bad: float) -> None:
    with pytest.raises(ValueError, match="max_fpr"):
        zero_fp_runs(bad)
    with pytest.raises(ValueError, match="min_recall"):
        zero_miss_runs(bad)
    with pytest.raises(ValueError, match="alpha"):
        zero_fp_runs(0.01, alpha=bad)


def test_alpha_too_small_for_double_precision_is_rejected() -> None:
    # 1 - 5e-17 rounds to 1, so the exact upper bound is 1 at every n; this
    # used to loop forever in zero_fp_runs and give nonsense counts elsewhere
    with pytest.raises(ValueError, match="too small"):
        zero_fp_runs(0.75, 5e-17)
    with pytest.raises(ValueError, match="too small"):
        events_allowed(1000, 0.5, alpha=5e-17)
    with pytest.raises(ValueError, match="too small"):
        split_alpha(5e-17)
    with pytest.raises(ValueError, match="too small"):
        more_runs_fpr(0, 10, 0.5, alpha=5e-17)


def test_targets_no_run_count_can_certify() -> None:
    # 2**60 runs with no false positive bound the FPR near 2.6e-18, above 1e-19
    with pytest.raises(ValueError, match="no number of runs"):
        zero_fp_runs(1e-19)
    assert int(events_allowed(10, 1e-19)) == -1
    assert float(fpr_power(10, 1e-19, 0.0)) == 0.0


# ------------------------------------------------------------------ events allowed and power


@pytest.mark.parametrize(
    ("metric", "bound"), [("fpr", 0.05), ("fpr", 0.2), ("recall", 0.9), ("recall", 0.6)]
)
def test_events_allowed_matches_brute_force(metric: Metric, bound: float) -> None:
    runs = np.arange(1, 301)
    got = events_allowed(runs, bound, metric)
    want = [brute_allowed(int(n), bound, metric) for n in runs]
    np.testing.assert_array_equal(got, want)


def test_events_allowed_hand_values_shapes_and_errors() -> None:
    # 299 runs certify FPR <= 1% with no false positive; one needs 473 runs
    np.testing.assert_array_equal(events_allowed([1, 298, 299, 472, 473], 0.01), [-1, -1, 0, 0, 1])
    np.testing.assert_array_equal(events_allowed([28, 29], 0.9, "recall"), [-1, 0])
    grid = events_allowed(np.array([[299, 473], [1, 1000]]), 0.01)
    assert grid.shape == (2, 2)
    np.testing.assert_array_equal(grid, [[0, 1], [-1, brute_allowed(1000, 0.01, "fpr")]])
    assert events_allowed(np.array([], dtype=np.int64), 0.01).shape == (0,)
    with pytest.raises(ValueError, match="at least 1"):
        events_allowed([0, 5], 0.01)
    with pytest.raises(ValueError, match="bound"):
        events_allowed(10, 1.0)
    with pytest.raises(ValueError, match="alpha"):
        events_allowed(10, 0.5, alpha=1.0)


def test_power_matches_the_binomial_sum() -> None:
    runs = np.array([1, 50, 299, 300, 600, 1000, 1500])
    got = fpr_power(runs, 0.01, 0.003)
    want = [brute_power(int(n), 0.01, 0.003, "fpr", ALPHA) for n in runs]
    np.testing.assert_allclose(got, want, rtol=1e-10, atol=1e-14)
    runs = np.array([1, 10, 29, 60, 100, 150])
    got = recall_power(runs, 0.8, 0.92, alpha=0.01)
    want = [brute_power(int(n), 0.8, 0.08, "recall", 0.01) for n in runs]
    np.testing.assert_allclose(got, want, rtol=1e-10, atol=1e-14)


def test_power_edges_and_errors() -> None:
    assert float(fpr_power(298, 0.01, 0.0)) == 0.0  # below the zero-event size
    assert float(fpr_power(299, 0.01, 0.0)) == 1.0
    assert float(recall_power(29, 0.9, 1.0)) == 1.0
    assert float(recall_power(28, 0.9, 1.0)) == 0.0
    with pytest.raises(ValueError, match="true_fpr"):
        fpr_power(100, 0.01, 1.5)
    with pytest.raises(ValueError, match="true_recall"):
        recall_power(100, 0.9, -0.1)


# ------------------------------------------------------------------ power-based sizes

PLANS: list[tuple[Metric, float, float, float, float]] = [
    ("fpr", 0.01, 0.002, 0.8, 0.05),
    ("fpr", 0.05, 0.02, 0.9, 0.05),
    ("fpr", 0.02, 0.01, 0.8, 0.1),
    ("recall", 0.9, 0.97, 0.8, 0.05),
    ("recall", 0.5, 0.7, 0.9, 0.05),
    ("recall", 0.8, 0.9, 0.8, 0.01),
]


def plan_for(metric: Metric, bound: float, assumed: float, power: float, alpha: float) -> RunPlan:
    if metric == "fpr":
        return fpr_runs_for_power(bound, assumed, power, alpha)
    return recall_runs_for_power(bound, assumed, power, alpha)


@pytest.mark.parametrize(("metric", "bound", "assumed", "power", "alpha"), PLANS)
def test_power_plan_is_exact_under_the_stays_above_rule(
    metric: Metric, bound: float, assumed: float, power: float, alpha: float
) -> None:
    plan = plan_for(metric, bound, assumed, power, alpha)
    assert plan.horizon is not None and plan.first_runs is not None
    n = np.arange(1, plan.horizon + 1)
    pw = power_curve(metric, n, bound, assumed, alpha)
    ok = pw >= power
    # the returned n meets the target at every n through the horizon ...
    assert np.all(ok[plan.runs - 1 :])
    # ... and n - 1 does not meet it, so no smaller n satisfies the rule
    assert not ok[plan.runs - 2]
    assert plan.horizon >= 2 * plan.runs
    assert plan.first_runs == int(n[np.argmax(ok)]) <= plan.runs
    assert plan.power == pytest.approx(pw[plan.runs - 1])
    assert plan.min_power == pytest.approx(pw[plan.runs - 1 :].min())
    assert plan.events_allowed == brute_allowed(plan.runs, bound, metric, alpha)
    zero = zero_fp_runs(bound, alpha) if metric == "fpr" else zero_miss_runs(bound, alpha)
    assert plan.runs >= zero
    assert (plan.metric, plan.bound, plan.alpha) == (metric, bound, alpha)
    assert (plan.assumed, plan.power_target) == (assumed, power)
    p_event = assumed if metric == "fpr" else 1 - assumed
    assert brute_power(plan.runs, bound, p_event, metric, alpha) == pytest.approx(plan.power)


def test_power_is_not_monotone_so_the_first_crossing_is_unsafe() -> None:
    plan = fpr_runs_for_power(0.01, 0.002, 0.8)
    assert plan.first_runs is not None and plan.first_runs < plan.runs
    pw = fpr_power(np.arange(plan.first_runs, plan.runs), 0.01, 0.002)
    assert pw[0] >= 0.8
    assert pw.min() < 0.8  # power falls below the target again after the first crossing


def test_power_plan_with_a_perfect_monitor_is_the_zero_event_size() -> None:
    plan = fpr_runs_for_power(0.01, 0.0, 0.8)
    assert (plan.runs, plan.events_allowed, plan.power) == (zero_fp_runs(0.01), 0, 1.0)
    plan = recall_runs_for_power(0.9, 1.0, 0.99)
    assert (plan.runs, plan.events_allowed, plan.power) == (zero_miss_runs(0.9), 0, 1.0)


def test_power_plans_reject_bad_inputs() -> None:
    with pytest.raises(ValueError, match="strictly below max_fpr"):
        fpr_runs_for_power(0.01, 0.01)
    with pytest.raises(ValueError, match="strictly below max_fpr"):
        fpr_runs_for_power(0.01, -0.001)
    with pytest.raises(ValueError, match="strictly above min_recall"):
        recall_runs_for_power(0.9, 0.9)
    with pytest.raises(ValueError, match="strictly above min_recall"):
        recall_runs_for_power(0.9, 1.2)
    with pytest.raises(ValueError, match="power"):
        fpr_runs_for_power(0.01, 0.002, power=1.0)
    with pytest.raises(ValueError, match="max_fpr"):
        fpr_runs_for_power(0.0, 0.002)
    # power stays above 0.8 only from 773 runs on
    with pytest.raises(ValueError, match="n_max=500"):
        fpr_runs_for_power(0.01, 0.002, n_max=500)
    with pytest.raises(ValueError, match="n_max"):
        recall_runs_for_power(0.9, 0.97, n_max=10)


# ------------------------------------------------------------------ joint plans


def test_split_alpha_bonferroni_and_sidak() -> None:
    assert split_alpha(0.05) == pytest.approx((0.025, 0.025))
    assert split_alpha(0.05, recall_share=0.8) == pytest.approx((0.04, 0.01))
    s_r, s_f = split_alpha(0.05, "sidak")
    assert (1 - s_r) * (1 - s_f) == pytest.approx(0.95)
    assert s_r == pytest.approx(s_f) and s_r > 0.025
    s_r, s_f = split_alpha(0.1, "sidak", recall_share=0.3)
    assert (1 - s_r) * (1 - s_f) == pytest.approx(0.9)
    assert s_r < s_f
    with pytest.raises(ValueError, match="split"):
        split_alpha(0.05, "holm")
    with pytest.raises(ValueError, match="alpha"):
        split_alpha(0.0)
    with pytest.raises(ValueError, match="recall_share"):
        split_alpha(0.05, recall_share=1.0)


def test_joint_plan_without_rates_uses_zero_event_arms_at_split_alpha() -> None:
    plan = joint_plan(0.9, 0.001)
    assert plan.alpha_recall + plan.alpha_fpr == pytest.approx(0.05)
    assert plan.alpha_recall == pytest.approx(0.025) and plan.split == "bonferroni"
    assert plan.n_pos == zero_miss_runs(0.9, 0.025) == 36
    assert plan.n_neg == zero_fp_runs(0.001, 0.025) == 3688
    assert plan.recall == RunPlan("recall", 0.9, 0.025, 36, 0)
    assert plan.fpr == RunPlan("fpr", 0.001, 0.025, 3688, 0)
    assert plan.joint_power is None and plan.power_target is None
    assert plan.note == CLUSTER_NOTE
    # splitting alpha costs runs relative to certifying either claim alone
    assert plan.n_neg > zero_fp_runs(0.001) and plan.n_pos > zero_miss_runs(0.9)


def test_joint_plan_with_both_rates_targets_the_joint_power() -> None:
    plan = joint_plan(0.8, 0.01, true_recall=0.9, true_fpr=0.002, power=0.8)
    arm = 0.8**0.5
    assert plan.recall == recall_runs_for_power(0.8, 0.9, arm, 0.025)
    assert plan.fpr == fpr_runs_for_power(0.01, 0.002, arm, 0.025)
    assert plan.recall.power is not None and plan.fpr.power is not None
    assert plan.joint_power == pytest.approx(plan.recall.power * plan.fpr.power)
    # independent samples: P(both certify) is the product of the arm powers
    want = recall_power(plan.n_pos, 0.8, 0.9, 0.025) * fpr_power(plan.n_neg, 0.01, 0.002, 0.025)
    assert plan.joint_power == pytest.approx(float(want))
    assert plan.joint_power is not None and plan.joint_power >= 0.8
    assert plan.power_target == 0.8


def test_joint_plan_with_one_rate_plans_only_that_arm_for_power() -> None:
    plan = joint_plan(0.9, 0.01, true_fpr=0.002, power=0.9)
    assert plan.recall == RunPlan("recall", 0.9, 0.025, zero_miss_runs(0.9, 0.025), 0)
    assert plan.fpr == fpr_runs_for_power(0.01, 0.002, 0.9, 0.025)
    assert plan.joint_power is None and plan.power_target == 0.9
    plan = joint_plan(0.9, 0.01, true_recall=0.97)
    assert plan.fpr.runs == zero_fp_runs(0.01, 0.025)
    assert plan.recall == recall_runs_for_power(0.9, 0.97, 0.8, 0.025)


def test_joint_plan_sidak_and_recall_share() -> None:
    bon = joint_plan(0.9, 0.001)
    sid = joint_plan(0.9, 0.001, split="sidak")
    assert (sid.alpha_recall, sid.alpha_fpr) == split_alpha(0.05, "sidak")
    assert sid.split == "sidak"
    assert sid.n_pos <= bon.n_pos and sid.n_neg <= bon.n_neg
    skew = joint_plan(0.9, 0.001, recall_share=0.2)
    assert (skew.alpha_recall, skew.alpha_fpr) == pytest.approx((0.01, 0.04))
    assert skew.n_pos == zero_miss_runs(0.9, skew.alpha_recall)
    assert skew.n_neg == zero_fp_runs(0.001, skew.alpha_fpr)


def test_joint_plan_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="split"):
        joint_plan(0.9, 0.01, split="holm")
    # checked before the arm power is taken (a negative power has a complex root)
    with pytest.raises(ValueError, match="power"):
        joint_plan(0.9, 0.01, true_recall=0.97, true_fpr=0.002, power=-0.5)
    with pytest.raises(ValueError, match="power"):
        joint_plan(0.9, 0.01, true_fpr=0.002, power=1.0)
    with pytest.raises(ValueError, match="strictly below max_fpr"):
        joint_plan(0.9, 0.01, true_fpr=0.02)
    with pytest.raises(ValueError, match="min_recall"):
        joint_plan(1.0, 0.01)


# ------------------------------------------------------------------ more runs


def test_more_runs_fpr_with_no_false_positive_so_far() -> None:
    out = more_runs_fpr(0, 100, 0.01, power=0.8)
    assert (out.metric, out.events, out.runs, out.bound, out.alpha) == ("fpr", 0, 100, 0.01, 0.05)
    assert out.current_bound == pytest.approx(1 - 0.05 ** (1 / 100))
    assert not out.certified and out.observed == 0.0 and out.reachable
    assert out.if_no_events == zero_fp_runs(0.01) - 100 == 199
    assert out.at_observed_rate == 199
    assert out.for_power == 199  # no future false positive at a rate of 0: power 1
    assert out.power_target == 0.8


def test_more_runs_fpr_answers_are_exact_when_reachable() -> None:
    k, n, f = 1, 300, 0.01
    out = more_runs_fpr(k, n, f, power=0.8)
    assert out.reachable and not out.certified
    assert out.observed == pytest.approx(1 / 300)
    assert out.current_bound == pytest.approx(
        float(intervals.clopper_pearson(1, 300, 0.05, side="upper")[1])
    )
    a, b, m = out.if_no_events, out.at_observed_rate, out.for_power
    assert b is not None and m is not None
    # (a) no false positive among the new runs: the smallest total that certifies
    assert certified("fpr", k, n + a, f, ALPHA) and not certified("fpr", k, n + a - 1, f, ALPHA)
    # (b) expected false positives at the observed rate, the smallest such total
    rate = k / n
    assert certified("fpr", rate * (n + b), n + b, f, ALPHA)
    assert not certified("fpr", rate * (n + b - 1), n + b - 1, f, ALPHA)
    # (c) binomial future false positives: power stays at or above the target
    # from m through 2 m, and m - 1 is below it
    curve = future_power("fpr", k, n, f, np.arange(m - 1, 2 * m + 1))
    assert curve[0] < 0.8 and np.all(curve[1:] >= 0.8)
    assert brute_future_power("fpr", k, n, f, m) == pytest.approx(curve[1])
    assert brute_future_power("fpr", k, n, f, m - 1) == pytest.approx(curve[0])


def test_more_runs_fpr_unreachable_at_the_observed_rate() -> None:
    out = more_runs_fpr(1, 100, 0.01, power=0.8)  # observed rate equals the target
    assert not out.reachable and not out.certified
    assert out.at_observed_rate is None and out.for_power is None
    assert out.if_no_events == 373
    assert certified("fpr", 1, 473, 0.01, ALPHA) and not certified("fpr", 1, 472, 0.01, ALPHA)
    out = more_runs_fpr(5, 100, 0.01)  # observed rate above the target
    assert not out.reachable and out.at_observed_rate is None and out.for_power is None
    assert out.power_target is None and out.observed == 0.05


def test_more_runs_fpr_already_certified() -> None:
    out = more_runs_fpr(2, 1000, 0.01, power=0.8)
    assert out.certified and out.current_bound <= 0.01
    assert (out.if_no_events, out.at_observed_rate, out.for_power) == (0, 0, 0)


def test_more_runs_without_power_and_search_limits() -> None:
    out = more_runs_fpr(1, 300, 0.01)
    assert out.for_power is None and out.power_target is None
    assert out.at_observed_rate == more_runs_fpr(1, 300, 0.01, power=0.8).at_observed_rate
    # (b) needs more than n_max runs: not found, though reachable
    out = more_runs_fpr(1, 300, 0.01, power=0.8, n_max=100)
    assert out.reachable and out.at_observed_rate is None and out.for_power is None
    assert out.if_no_events == 173  # (a) is never capped
    # (b) found, (c) beyond n_max
    full = more_runs_fpr(1, 300, 0.01, power=0.8)
    assert full.at_observed_rate is not None and full.for_power is not None
    out = more_runs_fpr(1, 300, 0.01, power=0.8, n_max=full.at_observed_rate)
    assert out.at_observed_rate == full.at_observed_rate and out.for_power is None


def test_more_runs_recall_cases() -> None:
    out = more_runs_recall(10, 10, 0.9, power=0.8)
    assert (out.metric, out.events, out.observed) == ("recall", 0, 1.0)
    assert out.current_bound == pytest.approx(0.05 ** (1 / 10))
    assert out.if_no_events == zero_miss_runs(0.9) - 10 == 19
    assert out.at_observed_rate == 19 and out.for_power == 19
    out = more_runs_recall(9, 10, 0.9, power=0.8)  # observed recall equals the target
    assert out.events == 1 and not out.reachable
    assert out.at_observed_rate is None and out.for_power is None
    out = more_runs_recall(0, 5, 0.5)  # nothing detected
    assert out.observed == 0.0 and not out.reachable and out.current_bound == 0.0
    out = more_runs_recall(200, 200, 0.9, power=0.8)
    assert out.certified and out.if_no_events == 0 and out.for_power == 0


def test_more_runs_recall_answers_are_exact_when_reachable() -> None:
    detected, n, r = 95, 100, 0.9
    out = more_runs_recall(detected, n, r, power=0.8)
    misses = n - detected
    assert out.events == misses and out.observed == 0.95 and out.reachable
    a, b, m = out.if_no_events, out.at_observed_rate, out.for_power
    assert b is not None and m is not None
    assert certified("recall", misses, n + a, r, ALPHA)
    assert not certified("recall", misses, n + a - 1, r, ALPHA)
    rate = misses / n
    assert certified("recall", rate * (n + b), n + b, r, ALPHA)
    assert not certified("recall", rate * (n + b - 1), n + b - 1, r, ALPHA)
    curve = future_power("recall", misses, n, r, np.arange(m - 1, 2 * m + 1))
    assert curve[0] < 0.8 and np.all(curve[1:] >= 0.8)
    assert brute_future_power("recall", misses, n, r, m) == pytest.approx(curve[1])


def test_more_runs_reject_bad_inputs() -> None:
    with pytest.raises(ValueError, match="runs >= 1"):
        more_runs_fpr(1, 0, 0.01)
    with pytest.raises(ValueError, match="events"):
        more_runs_fpr(5, 4, 0.01)
    with pytest.raises(ValueError, match="events"):
        more_runs_fpr(-1, 4, 0.01)
    with pytest.raises(ValueError, match="detected"):
        more_runs_recall(5, 4, 0.9)
    with pytest.raises(ValueError, match="runs >= 1"):
        more_runs_recall(0, 0, 0.9)
    with pytest.raises(ValueError, match="power"):
        more_runs_fpr(1, 300, 0.01, power=1.5)
    with pytest.raises(ValueError, match="bound"):
        more_runs_recall(9, 10, 1.0)
    with pytest.raises(ValueError, match="alpha"):
        more_runs_fpr(1, 300, 0.01, alpha=0.0)


# ------------------------------------------------------------------ budget mode

PREV, FLAG = 0.01, 0.02


@pytest.fixture(scope="module")
def pilot() -> tuple[FloatArray, FloatArray]:
    """Synthetic binormal pilot: benign N(0, 1), violations N(2.5, 1) (not monitor scores)."""
    rng = np.random.default_rng(20261104)
    return rng.normal(size=3000), rng.normal(2.5, 1.0, size=300)


def test_budget_power_is_deterministic_given_the_seed(
    pilot: tuple[FloatArray, FloatArray],
) -> None:
    neg, pos = pilot

    def run(seed: int, method: str, sims: int) -> BudgetDesign:
        return budget_power(
            neg,
            pos,
            PREV,
            FLAG,
            2000,
            100,
            min_recall=0.5,
            method=method,
            sims=sims,
            reps=30,
            seed=seed,
        )

    assert run(7, "cluster_cp", 100) == run(7, "cluster_cp", 100)
    assert run(7, "cluster_cp", 100) != run(8, "cluster_cp", 100)
    assert run(3, "composite", 3) == run(3, "composite", 3)


def test_budget_power_is_monotone_in_violation_runs(pilot: tuple[FloatArray, FloatArray]) -> None:
    neg, pos = pilot
    designs = [
        budget_power(
            neg,
            pos,
            PREV,
            FLAG,
            3000,
            n_pos,
            min_recall=0.45,
            method="cluster_cp",
            sims=400,
            seed=1,
        )
        for n_pos in (25, 50, 100, 200, 400)
    ]
    for small, large in itertools.pairwise(designs):
        # Monte Carlo slack of three combined standard errors
        assert large.power >= small.power - 3 * math.hypot(small.mcse, large.mcse)
        assert large.median_half_width < small.median_half_width
    assert designs[-1].power > designs[0].power + 0.3
    for d in designs:
        assert d.mcse == pytest.approx(math.sqrt(d.power * (1 - d.power) / d.sims))
        assert d.median_lower <= d.median_upper and d.sims == 400


def test_budget_power_half_width_target(pilot: tuple[FloatArray, FloatArray]) -> None:
    neg, pos = pilot
    powers = [
        budget_power(
            neg,
            pos,
            PREV,
            FLAG,
            3000,
            n_pos,
            half_width=0.08,
            method="plugin_wilson",
            sims=200,
            seed=2,
        ).power
        for n_pos in (30, 300)
    ]
    assert powers[0] < 0.2 and powers[1] > 0.8


@pytest.mark.parametrize("method", ["plugin_cp", "plugin_wilson", "plugin_jeffreys", "cluster_cp"])
@pytest.mark.parametrize("clustered", [False, True])
def test_count_weights_equal_the_expanded_studies(method: str, clustered: bool) -> None:
    rng = np.random.default_rng(5)
    neg = rng.normal(size=400)
    pos = rng.normal(2.0, 1.0, size=60)
    neg_c = np.repeat(np.arange(200), 2) if clustered else None
    pos_c = rng.integers(0, 25, size=60) if clustered else None  # unequal run sizes
    p_neg, p_pos = atbudget._pilot(neg, neg_c), atbudget._pilot(pos, pos_c)

    def sim(expand: bool) -> tuple[FloatArray, FloatArray]:
        return atbudget._simulated_intervals(
            p_neg, p_pos, 0.01, 0.05, 300, 40, method, 0.05, 30, 10, 3, expand
        )

    lo_batch, hi_batch = sim(False)
    lo_full, hi_full = sim(True)
    np.testing.assert_allclose(lo_batch, lo_full, atol=1e-9)
    np.testing.assert_allclose(hi_batch, hi_full, atol=1e-9)


def test_expand_repeats_whole_runs_with_new_labels() -> None:
    p = atbudget._pilot(
        np.array([10.0, 11.0, 20.0, 30.0, 31.0, 32.0]), ["a", "a", "b", "c", "c", "c"]
    )
    assert p.runs == 3 and not p.unclustered
    scores, labels = atbudget._expand(p, np.array([2, 0, 1], dtype=np.int64))
    np.testing.assert_array_equal(scores, [10, 11, 10, 11, 30, 31, 32])
    np.testing.assert_array_equal(labels, [0, 0, 1, 1, 2, 2, 2])
    assert atbudget._pilot([1.0, 2.0], None).unclustered


def test_budget_power_resampling_methods_with_clustered_pilots(
    pilot: tuple[FloatArray, FloatArray],
) -> None:
    neg, pos = pilot
    d = budget_power(
        neg[:600],
        pos[:80],
        PREV,
        FLAG,
        300,
        40,
        half_width=0.25,
        method="cluster_bootstrap",
        neg_clusters=np.repeat(np.arange(300), 2),
        pos_clusters=np.repeat(np.arange(40), 2),
        sims=3,
        reps=30,
        seed=4,
    )
    assert d.sims == 3 and 0.0 <= d.power <= 1.0
    assert d.median_lower <= d.median_upper
    d = budget_power(
        neg[:600],
        pos[:80],
        PREV,
        FLAG,
        600,
        80,
        min_recall=0.2,
        method="bayesian_bootstrap",
        sims=3,
        reps=30,
        seed=4,
    )
    assert (d.n_neg, d.n_pos) == (600, 80)


def test_plan_budget_picks_the_cheapest_design_meeting_the_target(
    pilot: tuple[FloatArray, FloatArray], monkeypatch: pytest.MonkeyPatch
) -> None:
    powers = {(100, 10): 0.5, (100, 20): 0.85, (110, 10): 0.9, (110, 20): 0.95}

    def fake_design(
        neg: object,
        pos: object,
        prevalence: float,
        flag_rate: float,
        n_neg: int,
        n_pos: int,
        *rest: object,
    ) -> BudgetDesign:
        p = powers[(n_neg, n_pos)]
        return BudgetDesign(n_neg, n_pos, p, 0.01, 0.4, 0.8, 0.2, 100)

    monkeypatch.setattr(atbudget, "_design", fake_design)
    neg, pos = pilot

    def best(power: float, neg_cost: float, pos_cost: float) -> tuple[int, int] | None:
        plan = plan_budget(
            neg,
            pos,
            PREV,
            FLAG,
            min_recall=0.5,
            power=power,
            n_neg_grid=(100, 110),
            n_pos_grid=(10, 20),
            neg_cost=neg_cost,
            pos_cost=pos_cost,
        )
        assert [(d.n_neg, d.n_pos) for d in plan.designs] == list(powers)
        return None if plan.best is None else (plan.best.n_neg, plan.best.n_pos)

    # (100, 20) and (110, 10) both cost 120: the tie goes to the higher power
    assert best(0.8, 1.0, 1.0) == (110, 10)
    assert best(0.8, 2.0, 1.0) == (100, 20)  # benign runs cost more
    assert best(0.92, 1.0, 1.0) == (110, 20)
    assert best(0.96, 1.0, 1.0) is None


def test_plan_budget_reports_the_pilot_and_is_deterministic(
    pilot: tuple[FloatArray, FloatArray],
) -> None:
    neg, pos = pilot

    def plan() -> BudgetPlan:
        return plan_budget(
            neg,
            pos,
            PREV,
            FLAG,
            min_recall=0.45,
            n_neg_grid=(1000, 3000),
            n_pos_grid=(50, 400),
            method="cluster_cp",
            sims=100,
            seed=4,
        )

    first = plan()
    assert first == plan()
    point = budget_point(neg, pos, PREV, FLAG)
    assert first.pilot == point
    assert first.pilot_tail_neg == int(np.sum(neg >= point.threshold))
    assert (first.method, first.alpha, first.seed, first.power_target) == (
        "cluster_cp",
        0.05,
        4,
        0.8,
    )
    assert first.min_recall == 0.45 and first.half_width is None
    assert first.best is not None and first.best.power >= 0.8
    cheaper = [d for d in first.designs if d.n_neg + d.n_pos < first.best.n_neg + first.best.n_pos]
    assert all(d.power < 0.8 for d in cheaper)


def test_budget_planning_rejects_bad_inputs(pilot: tuple[FloatArray, FloatArray]) -> None:
    neg, pos = pilot
    cases: list[tuple[str, Callable[[], object]]] = [
        ("exactly one", lambda: budget_power(neg, pos, PREV, FLAG, 100, 10)),
        (
            "exactly one",
            lambda: budget_power(neg, pos, PREV, FLAG, 100, 10, min_recall=0.5, half_width=0.1),
        ),
        ("min_recall", lambda: budget_power(neg, pos, PREV, FLAG, 100, 10, min_recall=1.0)),
        ("half_width", lambda: budget_power(neg, pos, PREV, FLAG, 100, 10, half_width=0.0)),
        (
            "method",
            lambda: budget_power(neg, pos, PREV, FLAG, 100, 10, min_recall=0.5, method="bogus"),
        ),
        ("sims", lambda: budget_power(neg, pos, PREV, FLAG, 100, 10, min_recall=0.5, sims=0)),
        ("at least 1", lambda: budget_power(neg, pos, PREV, FLAG, 0, 10, min_recall=0.5)),
        ("pilot", lambda: budget_power([], pos, PREV, FLAG, 100, 10, min_recall=0.5)),
        ("power", lambda: plan_budget(neg, pos, PREV, FLAG, min_recall=0.5, power=1.0)),
        ("grids", lambda: plan_budget(neg, pos, PREV, FLAG, min_recall=0.5, n_neg_grid=())),
        ("exactly one", lambda: plan_budget(neg, pos, PREV, FLAG)),
        ("sims", lambda: plan_budget(neg, pos, PREV, FLAG, min_recall=0.5, sims=0)),
    ]
    for pattern, call in cases:
        with pytest.raises(ValueError, match=pattern):
            call()
