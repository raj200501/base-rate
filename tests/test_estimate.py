"""Tests for baserate.estimate: intervals, operating points, budget and fixed estimators."""

from __future__ import annotations

import json
import warnings

import numpy as np
import pytest

from baserate.estimate import (
    BUDGET_METHODS,
    FIXED_METHODS,
    Interval,
    UnsupportedCellWarning,
    bootstrap,
    budget_point,
    fixed_threshold,
    flag_rate_from_budget,
    intervals,
    operating,
    rate_interval,
    recall_at_budget,
    support,
)

# ---------------------------------------------------------------- intervals


def test_clopper_pearson_known_values() -> None:
    lo, hi = intervals.clopper_pearson(0, 10, 0.05)
    assert lo == 0.0
    assert hi == pytest.approx(1 - 0.025 ** (1 / 10))
    lo, hi = intervals.clopper_pearson(10, 10, 0.05)
    assert hi == 1.0
    assert lo == pytest.approx(0.025 ** (1 / 10))
    lo, hi = intervals.clopper_pearson(5, 10)
    assert lo == pytest.approx(0.187086, abs=1e-5)
    assert hi == pytest.approx(0.812914, abs=1e-5)


def test_one_sided_bounds() -> None:
    lo, hi = intervals.clopper_pearson(0, 100, 0.05, side="upper")
    assert lo == 0.0
    assert hi == pytest.approx(1 - 0.05 ** (1 / 100))
    lo, hi = intervals.clopper_pearson(100, 100, 0.05, side="lower")
    assert hi == 1.0
    assert lo == pytest.approx(0.05 ** (1 / 100))


def test_zero_event_upper_is_about_three_over_n() -> None:
    assert intervals.zero_event_upper(1000) == pytest.approx(3 / 1000, rel=0.01)
    with pytest.raises(ValueError):
        intervals.zero_event_upper(0)


def test_wilson_and_jeffreys_are_vectorized_and_ordered() -> None:
    k = np.array([0, 1, 5, 10])
    for fn in (intervals.wilson, intervals.jeffreys):
        lo, hi = fn(k, 10)
        assert lo.shape == (4,)
        assert np.all(lo <= k / 10 + 1e-12)
        assert np.all(hi >= k / 10 - 1e-12)
    lo, hi = intervals.jeffreys(np.array([0, 10]), 10)
    assert lo[0] == 0.0 and hi[1] == 1.0


def test_beta_posterior_prior_and_errors() -> None:
    lo, hi = intervals.beta_posterior(3, 10, prior=(2.0, 2.0))
    assert 0 < lo < hi < 1
    with pytest.raises(ValueError):
        intervals.beta_posterior(3, 10, prior=(0.0, 1.0))
    with pytest.raises(ValueError):
        intervals.clopper_pearson(11, 10)
    with pytest.raises(ValueError):
        intervals.clopper_pearson(1, 0)
    with pytest.raises(ValueError):
        intervals.wilson(1, 10, alpha=1.5)


# ---------------------------------------------------------- operating point


def test_flag_rate_from_budget() -> None:
    assert flag_rate_from_budget(20) == pytest.approx(0.002)
    with pytest.raises(ValueError):
        flag_rate_from_budget(-1)


def test_hand_computed_operating_point_with_ties() -> None:
    # benign [0, 0, 0, 1], violations [1, 1], prevalence 0.5, review half of traffic.
    # Level 1 holds mass 0.5 * 1/4 + 0.5 * 1 = 0.625, so 0.5 / 0.625 = 0.8 of it is flagged.
    pt = budget_point([0, 0, 0, 1], [1, 1], prevalence=0.5, flag_rate=0.5)
    assert pt.threshold == 1.0
    assert pt.tie_fraction == pytest.approx(0.8)
    assert pt.recall == pytest.approx(0.8)
    assert pt.fpr == pytest.approx(0.2)
    assert pt.precision == pytest.approx(0.8)


def test_perfect_separation_catches_everything_when_budget_exceeds_prevalence() -> None:
    rng = np.random.default_rng(1)
    neg = rng.uniform(0, 0.5, 5000)
    pos = rng.uniform(0.6, 1.0, 200)
    pt = budget_point(neg, pos, prevalence=1e-3, flag_rate=2e-3)
    assert pt.recall == pytest.approx(1.0)
    assert pt.precision == pytest.approx(0.5)


def test_budget_below_prevalence_caps_recall() -> None:
    neg = np.zeros(100)
    pos = np.ones(50)
    pt = budget_point(neg, pos, prevalence=0.01, flag_rate=0.005)
    assert pt.recall == pytest.approx(0.5)
    assert pt.precision == pytest.approx(1.0)
    assert pt.fpr == pytest.approx(0.0)


def test_flag_everything() -> None:
    pt = budget_point([0.1, 0.2], [0.3], prevalence=0.1, flag_rate=1.0)
    assert pt.recall == pytest.approx(1.0)
    assert pt.fpr == pytest.approx(1.0)


def test_integer_weights_equal_duplication() -> None:
    rng = np.random.default_rng(2)
    neg = rng.normal(size=300)
    pos = rng.normal(1.5, 1, size=40)
    w = rng.integers(1, 4, size=300)
    a = budget_point(neg, pos, 0.01, 0.02, neg_weights=w)
    b = budget_point(np.repeat(neg, w), pos, 0.01, 0.02)
    assert a.recall == pytest.approx(b.recall)
    assert a.threshold == pytest.approx(b.threshold)


def test_monotone_transform_invariance() -> None:
    rng = np.random.default_rng(3)
    neg = rng.normal(size=500)
    pos = rng.normal(2, 1, size=60)
    a = budget_point(neg, pos, 1e-3, 5e-3)
    b = budget_point(1 / (1 + np.exp(-neg)), 1 / (1 + np.exp(-pos)), 1e-3, 5e-3)
    assert a.recall == pytest.approx(b.recall)


def test_batch_matches_single_points() -> None:
    rng = np.random.default_rng(4)
    neg = rng.normal(size=200)
    pos = rng.normal(1, 1, size=30)
    wn = rng.uniform(0.5, 2, size=(5, 200))
    rec, thr, _ = operating.budget_recall_batch(neg, pos, 0.01, 0.05, wn, None)
    for i in range(5):
        assert rec[i] == pytest.approx(budget_point(neg, pos, 0.01, 0.05, neg_weights=wn[i]).recall)
    assert thr.shape == (5,)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"prevalence": 0.0, "flag_rate": 0.1}, "prevalence"),
        ({"prevalence": 0.1, "flag_rate": 0.0}, "flag_rate"),
    ],
)
def test_operating_point_validation(kwargs: dict[str, float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        budget_point([0.1], [0.2], **kwargs)


def test_operating_point_input_errors() -> None:
    with pytest.raises(ValueError, match="at least one"):
        budget_point([], [0.2], 0.1, 0.1)
    with pytest.raises(ValueError, match="finite"):
        budget_point([np.nan], [0.2], 0.1, 0.1)
    with pytest.raises(ValueError, match="columns"):
        operating.budget_recall_batch([0.1, 0.2], [0.3], 0.1, 0.1, np.ones((2, 3)))
    with pytest.raises(ValueError, match="non-negative"):
        operating.budget_recall_batch([0.1], [0.3], 0.1, 0.1, -np.ones(1))
    with pytest.raises(ValueError, match="positive total"):
        operating.budget_recall_batch([0.1], [0.3], 0.1, 0.1, np.zeros(1))


def test_precision_and_fpr_maps() -> None:
    assert operating.precision_from_recall(0.5, 1e-3, 1e-3) == pytest.approx(0.5)
    assert operating.fpr_from_recall(1.0, 0.5, 0.25) == pytest.approx(0.0)


# ---------------------------------------------------------------- bootstrap


def test_cluster_index_and_weights() -> None:
    idx = bootstrap.cluster_index(["a", "b", "a", "c"], 4)
    assert idx.tolist() == [0, 1, 0, 2]
    assert bootstrap.cluster_index(None, 3).tolist() == [0, 1, 2]
    with pytest.raises(ValueError):
        bootstrap.cluster_index(["a"], 2)
    rng = np.random.default_rng(5)
    w = bootstrap.cluster_weights(idx, 7, rng, "multinomial")
    assert w.shape == (7, 4)
    assert np.all(w[:, 0] == w[:, 2])  # traces in one cluster share weight
    wb = bootstrap.cluster_weights(idx, 7, rng, "bayesian")
    assert np.allclose(wb[:, [0, 1, 3]].sum(axis=1), 3)
    with pytest.raises(ValueError):
        bootstrap.cluster_weights(idx, 2, rng, "other")  # type: ignore[arg-type]  # invalid kind on purpose
    with pytest.raises(ValueError):
        bootstrap.cluster_weights(np.array([], dtype=np.intp), 2, rng)


def test_chunks_and_percentile() -> None:
    assert sum(bootstrap.chunks(10, 3, max_cells=6)) == 10
    assert list(bootstrap.chunks(5, 10**9)) == [1] * 5
    lo, hi = bootstrap.percentile_interval(np.linspace(0, 1, 101), 0.1)
    assert lo == pytest.approx(0.05) and hi == pytest.approx(0.95)
    with pytest.raises(ValueError):
        bootstrap.percentile_interval(np.array([]))


# ------------------------------------------------------------ recall at budget


def _sample(
    rng: np.random.Generator, n0: int = 2000, n1: int = 100
) -> tuple[np.ndarray, np.ndarray]:
    return rng.normal(size=n0), rng.normal(2.5, 1, size=n1)


@pytest.mark.parametrize("method", BUDGET_METHODS)
def test_every_budget_method_brackets_its_estimate(method: str) -> None:
    rng = np.random.default_rng(6)
    neg, pos = _sample(rng)
    est = recall_at_budget(neg, pos, 1e-3, 0.01, method=method, reps=200, rng=rng)
    for iv in (est.recall, est.precision, est.fpr):
        assert iv.lower <= iv.estimate <= iv.upper
        assert iv.lower >= 0.0 and iv.upper <= 1.0
    assert est.n_neg == 2000 and est.n_pos == 100
    assert est.precision.estimate == pytest.approx(1e-3 * est.recall.estimate / 0.01)


def test_composite_is_wider_than_plugin() -> None:
    rng = np.random.default_rng(7)
    neg, pos = _sample(rng, n0=1000)
    plug = recall_at_budget(neg, pos, 1e-3, 2e-3, method="plugin_cp")
    comp = recall_at_budget(neg, pos, 1e-3, 2e-3, method="composite", reps=300, rng=rng)
    assert comp.recall.upper - comp.recall.lower > plug.recall.upper - plug.recall.lower


def test_clusters_reduce_effective_sample() -> None:
    rng = np.random.default_rng(8)
    neg, pos = _sample(rng)
    pos_clusters = [f"run{i // 10}" for i in range(pos.size)]
    plain = recall_at_budget(neg, pos, 1e-2, 0.02, method="plugin_cp")
    clus = recall_at_budget(neg, pos, 1e-2, 0.02, method="cluster_cp", pos_clusters=pos_clusters)
    assert clus.n_pos_clusters == 10
    assert clus.recall.upper - clus.recall.lower > plain.recall.upper - plain.recall.lower


def test_unknown_method_rejected() -> None:
    with pytest.raises(ValueError, match="method"):
        recall_at_budget([0.1], [0.2], 0.1, 0.1, method="magic")


def test_small_coverage_simulation_for_composite() -> None:
    # A quick sanity check, not the E0 study: the composite interval covers the
    # true recall at a budget in most replicates of a binormal model.
    from scipy import stats

    prevalence, flag_rate, d = 1e-2, 0.02, 2.0
    # true threshold t solves prevalence * S1(t) + (1 - prevalence) * S0(t) = flag_rate
    grid = np.linspace(-2, 6, 20001)
    tail = prevalence * stats.norm.sf(grid - d) + (1 - prevalence) * stats.norm.sf(grid)
    t_true = grid[np.argmin(np.abs(tail - flag_rate))]
    r_true = float(stats.norm.sf(t_true - d))
    rng = np.random.default_rng(9)
    hits = 0
    reps = 60
    for _ in range(reps):
        neg, pos = rng.normal(size=3000), rng.normal(d, 1, size=150)
        est = recall_at_budget(neg, pos, prevalence, flag_rate, "composite", reps=200, rng=rng)
        hits += est.recall.lower <= r_true <= est.recall.upper
    assert hits / reps >= 0.85


def test_unsupported_cells_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    cells = (
        {
            "id": "c1",
            "estimator": "plugin_cp",
            "prevalence": 1e-4,
            "flag_rate": 5e-4,
            "n_neg": 20000,
            "n_pos": 500,
            "clustered": False,
        },
    )
    monkeypatch.setattr(support, "_cells", lambda: cells)
    rng = np.random.default_rng(10)
    neg, pos = _sample(rng, n0=20000, n1=500)
    with pytest.warns(UnsupportedCellWarning, match="c1"):
        est = recall_at_budget(neg, pos, 1e-4, 5e-4, method="plugin_cp")
    assert est.warnings == ("c1",)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        quiet = recall_at_budget(neg, pos, 1e-4, 5e-4, method="plugin_cp", warn=False)
    assert quiet.warnings == ()
    assert support.check("cluster_bootstrap", 1e-4, 5e-4, 20000, 500, False) == ()


def test_packaged_unsupported_file_is_valid_json() -> None:
    from importlib import resources

    text = resources.files("baserate.estimate").joinpath("unsupported_cells.json").read_text()
    assert isinstance(json.loads(text)["cells"], list)


def test_interval_rejects_inverted_bounds() -> None:
    with pytest.raises(ValueError):
        Interval(0.5, 0.6, 0.4, "x", 0.05)


# ------------------------------------------------------------ fixed threshold


@pytest.mark.parametrize("method", FIXED_METHODS)
def test_rate_interval_methods(method: str) -> None:
    flags = np.array([True] * 3 + [False] * 97)
    clusters = [f"r{i // 5}" for i in range(100)]
    iv = rate_interval(flags, clusters, method=method, reps=200)
    assert iv.estimate == pytest.approx(0.03)
    assert iv.lower <= 0.03 <= iv.upper


def test_cluster_cp_zero_events_respects_independent_runs() -> None:
    flags = np.zeros(10_000, dtype=bool)
    clusters = [f"run{i // 10}" for i in range(10_000)]  # 1,000 independent runs
    iv = rate_interval(flags, clusters, method="cluster_cp")
    assert iv.upper == pytest.approx(1 - 0.025 ** (1 / 1000))
    naive = rate_interval(flags, None, method="cp")
    assert iv.upper > naive.upper * 5


def test_cluster_cp_brackets_mixed_clusters() -> None:
    flags = np.array([True, False, True, True, False, False])
    iv = rate_interval(flags, ["a", "a", "b", "b", "c", "c"], method="cluster_cp")
    assert iv.lower <= 0.5 <= iv.upper


def test_rate_interval_errors() -> None:
    with pytest.raises(ValueError):
        rate_interval([], method="cp")
    with pytest.raises(ValueError):
        rate_interval([True], method="nope")


def test_fixed_threshold_metrics() -> None:
    neg = np.r_[np.zeros(990), np.ones(10)]
    pos = np.r_[np.ones(90), np.zeros(10)]
    est = fixed_threshold(neg, pos, 0.5, prevalence=0.01, method="cp")
    assert est.recall.estimate == pytest.approx(0.9)
    assert est.fpr.estimate == pytest.approx(0.01)
    expected = 0.01 * 0.9 / (0.01 * 0.9 + 0.99 * 0.01)
    assert est.precision.estimate == pytest.approx(expected)
    assert est.precision.lower <= expected <= est.precision.upper
    with pytest.raises(ValueError):
        fixed_threshold(neg, pos, 0.5, prevalence=0.0)


def test_precision_is_zero_when_nothing_flagged() -> None:
    est = fixed_threshold(np.zeros(10), np.zeros(5), 0.5, prevalence=0.1, method="cp")
    assert est.precision.estimate == 0.0


# ------------------------------------------------------------- tail shortcut


def test_tail_computation_matches_full_weights() -> None:
    rng = np.random.default_rng(11)
    neg, pos = rng.normal(size=3000), rng.normal(2, 1, size=200)
    wn = rng.poisson(1.0, size=(20, neg.size)).astype(float) + 0.01
    wp = rng.poisson(1.0, size=(20, pos.size)).astype(float) + 0.01
    full, _, _ = operating.budget_recall_batch(neg, pos, 1e-3, 2e-3, wn, wp)
    cutoff = np.quantile(np.r_[neg, pos], 0.9)
    sn, sp = neg >= cutoff, pos >= cutoff
    tail, ok = operating.budget_recall_tail(
        neg[sn], pos[sp], 1e-3, 2e-3, wn[:, sn], wp[:, sp], wn.sum(axis=1), wp.sum(axis=1)
    )
    assert ok.all()
    assert np.allclose(tail, full)


def test_tail_reports_rows_where_threshold_is_below_cutoff() -> None:
    neg, pos = np.linspace(0, 1, 100), np.array([0.5])
    _, ok = operating.budget_recall_tail(
        neg[-2:], np.array([]), 0.01, 0.5, np.ones((1, 2)), np.ones((1, 0)), [100.0], [1.0]
    )
    assert not ok.any()
    assert pos.size == 1


def test_tail_draw_aggregates_exactly() -> None:
    idx = np.repeat(np.arange(50), 4).astype(np.intp)
    base = np.ones(idx.size)
    sel = np.zeros(idx.size, dtype=bool)
    sel[:8] = True  # clusters 0 and 1
    rng = np.random.default_rng(12)
    for kind in ("multinomial", "bayesian"):
        out = bootstrap.tail_draw(idx, base, sel, 4000, rng, kind)  # type: ignore[arg-type]  # literal from loop
        assert out is not None
        w, totals = out
        assert w.shape == (4000, 8)
        assert np.all(w[:, 0] == w[:, 3])
        if kind == "multinomial":
            assert np.allclose(totals, 200.0)
        else:
            assert np.allclose(totals, 200.0)
        assert w[:, 0].mean() == pytest.approx(1.0, abs=0.06)
    uneven = np.r_[np.ones(4), np.ones(4) * 2, np.ones(192)]
    assert (
        bootstrap.tail_draw(idx, uneven, np.r_[np.ones(4, bool), np.zeros(196, bool)], 3, rng)
        is None
    )
    with pytest.raises(ValueError):
        bootstrap.tail_draw(idx, base, sel, 2, rng, "x")  # type: ignore[arg-type]  # invalid kind on purpose


def test_shortcut_and_full_bootstrap_agree_in_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    from baserate.estimate import atbudget

    rng = np.random.default_rng(13)
    neg, pos = rng.normal(size=4000), rng.normal(2, 1, size=150)
    cl = np.repeat(np.arange(400), 10).astype(str)
    fast = recall_at_budget(
        neg,
        pos,
        1e-3,
        5e-3,
        "cluster_bootstrap",
        neg_clusters=cl,
        reps=2000,
        rng=np.random.default_rng(1),
        warn=False,
    )
    monkeypatch.setattr(atbudget, "tail_draw", lambda *a, **k: None)
    slow = recall_at_budget(
        neg,
        pos,
        1e-3,
        5e-3,
        "cluster_bootstrap",
        neg_clusters=cl,
        reps=2000,
        rng=np.random.default_rng(2),
        warn=False,
    )
    assert fast.recall.lower == pytest.approx(slow.recall.lower, abs=0.02)
    assert fast.recall.upper == pytest.approx(slow.recall.upper, abs=0.02)


def test_tail_cutoff_none_when_mass_covers_everything() -> None:
    from baserate.estimate import atbudget

    neg, pos = np.array([0.1, 0.2]), np.array([0.3])
    assert atbudget._tail_cutoff(neg, pos, np.ones(2), np.ones(1), 0.1, 1.0) is None
    assert atbudget._tail_cutoff(neg, pos, np.ones(2), np.ones(1), 0.1, 0.99) is None


def test_cluster_bootstrap_rate_with_unequal_clusters() -> None:
    flags = np.array([True, False, False, True, False, False, False])
    clusters = ["a", "a", "b", "c", "c", "c", "d"]
    iv = rate_interval(flags, clusters, method="cluster_bootstrap", reps=300)
    assert iv.lower <= iv.estimate <= iv.upper


def test_weights_trigger_the_unvalidated_design_warning() -> None:
    rng = np.random.default_rng(14)
    neg, pos = rng.normal(size=500), rng.normal(2, 1, size=50)
    with pytest.warns(UnsupportedCellWarning, match="weighted designs"):
        est = recall_at_budget(neg, pos, 1e-2, 0.02, "plugin_cp", neg_weights=np.ones(500))
    assert support.WEIGHTED in est.warnings
