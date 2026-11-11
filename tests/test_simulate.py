"""Tests for the E0 simulation models and harness."""

from __future__ import annotations

import numpy as np
import pytest

from baserate.estimate import budget_point, simulate


@pytest.mark.parametrize("name", ["binormal_d1", "binormal_d3", "heavy_tail"])
def test_continuous_truth_solves_the_budget_equation(name: str) -> None:
    model = simulate.MODELS[name]
    tr = simulate.truth(model, 1e-3, 2e-3)
    flagged = 1e-3 * model.pos.sf(tr.threshold) + (1 - 1e-3) * model.neg.sf(tr.threshold)
    assert flagged == pytest.approx(2e-3, rel=1e-9)
    assert tr.budget_recall == pytest.approx(model.pos.sf(tr.threshold))
    assert tr.fixed_recall == tr.budget_recall


def test_discrete_truth_matches_large_sample() -> None:
    model = simulate.MODELS["discrete4"]
    tr = simulate.truth(model, 1e-2, 0.01)
    rng = np.random.default_rng(0)
    neg = model.neg_from_latent(rng.normal(size=400_000))
    pos = model.sample_pos(rng, 400_000)
    pt = budget_point(neg, pos, 1e-2, 0.01)
    assert pt.recall == pytest.approx(tr.budget_recall, abs=0.01)
    assert np.mean(neg >= tr.threshold) == pytest.approx(tr.fixed_fpr, abs=0.002)
    assert np.mean(pos >= tr.threshold) == pytest.approx(tr.fixed_recall, abs=0.005)
    values, p0, p1 = model.level_probs()
    assert values[0] == 1.0 and p0.sum() == pytest.approx(1.0) and p1.sum() == pytest.approx(1.0)


def test_sample_shapes_and_cluster_correlation() -> None:
    rng = np.random.default_rng(1)
    design = simulate.DESIGNS["n20000c10_p500"]
    neg, clusters, pos = simulate.sample(simulate.MODELS["binormal_d3"], design, rng)
    assert neg.shape == (20000,) and pos.shape == (500,)
    assert clusters.max() + 1 == 2000
    means = neg.reshape(2000, 10).mean(axis=1)
    # with ICC 0.5 the variance of cluster means is about 0.5 + 0.5 / 10
    assert means.var() == pytest.approx(0.55, abs=0.08)
    assert neg.var() == pytest.approx(1.0, abs=0.1)
    assert design.n_neg == 20000


def test_run_cell_records_every_estimator() -> None:
    model, design = simulate.MODELS["binormal_d3"], simulate.DESIGNS["n2000_p100"]
    calls: list[int] = []
    tr, results = simulate.run_cell(
        model,
        design,
        1e-2,
        0.01,
        simulate.ESTIMATORS,
        replicates=3,
        bootstrap_replicates=2,
        resamples=50,
        seed=np.random.SeedSequence(5),
        progress=calls.append,
    )
    assert calls == [0, 1, 2]
    assert {r.estimator for r in results} == set(simulate.ESTIMATORS)
    for r in results:
        expected = 2 if simulate.refits_by_bootstrap(r.estimator) else 3
        assert r.hits.size == expected == r.widths.size
        assert np.all(r.widths >= 0)
    assert 0 < tr.budget_recall < 1


def test_run_cell_is_deterministic_given_seed() -> None:
    model, design = simulate.MODELS["discrete4"], simulate.DESIGNS["n2000_p100"]
    args = (
        model,
        design,
        1e-3,
        0.002,
        ("budget_recall:composite", "fixed_fpr:cluster_cp"),
        2,
        2,
        50,
    )
    _, a = simulate.run_cell(*args, seed=np.random.SeedSequence(7))
    _, b = simulate.run_cell(*args, seed=np.random.SeedSequence(7))
    assert all(np.array_equal(x.widths, y.widths) for x, y in zip(a, b, strict=True))


def test_coverage_criterion() -> None:
    assert simulate.coverage_passes(1900, 2000, 0.95, 0.015)
    assert simulate.coverage_passes(1880, 2000, 0.95, 0.015)  # 0.94 within tolerance
    assert not simulate.coverage_passes(1800, 2000, 0.95, 0.015)
    assert simulate.refits_by_bootstrap("budget_recall:composite")
    assert not simulate.refits_by_bootstrap("fixed_fpr:cluster_cp")
