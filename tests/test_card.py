"""Tests for baserate.card: every number on the card comes from the library."""

from __future__ import annotations

import json
import math
import warnings
from importlib import resources

import numpy as np
import pytest

from baserate import card as card_mod
from baserate.card import (
    CARD_BUDGETS,
    CARD_PREVALENCES,
    CLUSTER_AWARE_METHODS,
    SCHEMA,
    WEIGHTS_OMITTED,
    budget_table,
    build_card,
    certification_gap,
    confidence_text,
    e0_cell_text,
    e0_support_source,
    fmt_interval,
    fmt_prob,
    fmt_small,
    jsonable,
    method_note,
    render_json,
    render_markdown,
    summarize,
    threshold_summary,
)
from baserate.certify import more_runs_fpr, zero_fp_runs
from baserate.estimate import (
    Interval,
    UnsupportedCellWarning,
    fixed_threshold,
    flag_rate_from_budget,
    recall_at_budget,
    support,
)
from baserate.estimate.intervals import clopper_pearson, zero_event_upper
from baserate.scores import ScoreSet

EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)


def _clustered(seed: int = 3, weights: bool = False, synthetic: bool = True) -> ScoreSet:
    """Synthetic test scores: 40 benign runs of 3 traces, 15 violation runs of 2."""
    rng = np.random.default_rng(seed)
    neg_c = np.repeat([f"n{i}" for i in range(40)], 3)
    pos_c = np.repeat([f"p{i}" for i in range(15)], 2)
    neg = rng.beta(2, 6, neg_c.size)
    pos = rng.beta(6, 2, pos_c.size)
    n = neg.size + pos.size
    return ScoreSet(
        trace_ids=tuple(f"t{i}" for i in range(n)),
        labels=("benign",) * neg.size + ("violation",) * pos.size,
        scores=np.r_[neg, pos],
        clusters=tuple(neg_c.tolist() + pos_c.tolist()),
        weights=rng.uniform(0.5, 2.0, n) if weights else None,
        synthetic=np.arange(n) % 2 == 0 if synthetic else None,
        source="synthetic-test-scores",
    )


def _independent(n_neg: int, flagged_neg: int, n_pos: int = 30) -> ScoreSet:
    """Each trace its own run; ``flagged_neg`` benign traces score 0.9, the rest 0.1."""
    neg = np.full(n_neg, 0.1)
    neg[:flagged_neg] = 0.9
    pos = np.linspace(0.6, 1.0, n_pos)
    ids = tuple(f"t{i}" for i in range(n_neg + n_pos))
    return ScoreSet(
        trace_ids=ids,
        labels=("benign",) * n_neg + ("violation",) * n_pos,
        scores=np.r_[neg, pos],
        clusters=ids,
    )


def test_budget_rows_match_recall_at_budget() -> None:
    s = _clustered()
    rows = budget_table(s, reps=200, seed=4)
    assert len(rows) == len(CARD_PREVALENCES) * len(CARD_BUDGETS)
    for row in rows:
        est = recall_at_budget(
            s.neg,
            s.pos,
            row.prevalence,
            flag_rate_from_budget(row.budget_per_10k),
            "composite",
            neg_clusters=s.neg_clusters,
            pos_clusters=s.pos_clusters,
            reps=200,
            rng=np.random.default_rng(4),
        )
        assert row.recall == est.recall
        assert row.precision == est.precision
        assert row.fpr == est.fpr
        assert (row.n_neg, row.n_pos) == (est.n_neg, est.n_pos)
        assert (row.n_neg_clusters, row.n_pos_clusters) == (40, 15)
        assert row.threshold == est.point.threshold
        assert row.warnings == ()


def test_budget_rows_do_not_depend_on_other_rows() -> None:
    s = _clustered()
    full = budget_table(s, reps=200)
    one = budget_table(s, prevalences=[1e-3], budgets=[20], reps=200)
    match = [r for r in full if r.prevalence == 1e-3 and r.budget_per_10k == 20]
    assert one[0].recall == match[0].recall


def test_budget_table_uses_weights() -> None:
    s = _clustered(weights=True)
    row = budget_table(s, prevalences=[1e-2], budgets=[100], method="plugin_cp")[0]
    est = recall_at_budget(
        s.neg,
        s.pos,
        1e-2,
        0.01,
        "plugin_cp",
        neg_clusters=s.neg_clusters,
        pos_clusters=s.pos_clusters,
        neg_weights=s.neg_weights,
        pos_weights=s.pos_weights,
    )
    assert row.recall == est.recall


def test_budget_table_rejects_unknown_method() -> None:
    with pytest.raises(ValueError, match="method must be one of"):
        budget_table(_clustered(), method="nope")


def test_e0_warnings_land_on_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    cells = (
        {
            "id": "cellA",
            "estimator": "composite",
            "prevalence": 1e-3,
            "flag_rate": 0.002,
            "n_neg": 120,
            "n_pos": 30,
            "clustered": True,
        },
    )
    monkeypatch.setattr(support, "_cells", lambda: cells)
    with warnings.catch_warnings():
        warnings.simplefilter("error", UnsupportedCellWarning)
        rows = budget_table(_clustered(), prevalences=[1e-3], budgets=[20], reps=100)
    assert rows[0].warnings == ("cellA",)
    c = build_card(_clustered(), "m", prevalences=[1e-3], budgets=[20], reps=100)
    md = render_markdown(c)
    assert "composite under-covered in E0 cell cellA (docs/UNSUPPORTED.md)" in md
    assert json.loads(render_json(c))["budget_table"][0]["e0_unsupported_cells"] == ["cellA"]


def test_summary_counts_and_fpr_floor() -> None:
    s = _clustered()
    d = summarize(s)
    assert (d.n_traces, d.n_neg, d.n_pos) == (150, 120, 30)
    assert (d.n_neg_clusters, d.n_pos_clusters, d.n_clusters) == (40, 15, 55)
    assert d.n_synthetic == 75 and d.synthetic_share == pytest.approx(0.5)
    assert d.fpr_floor == pytest.approx(float(zero_event_upper(40, 0.05)))
    assert not d.weighted and d.n_stage2 is None
    assert d.source == "synthetic-test-scores" and d.sha256 is None


def test_threshold_summary_counts_clusters() -> None:
    s = _clustered()
    t = threshold_summary(s, 0.5)
    neg_flags = s.neg >= 0.5
    pos_flags = s.pos >= 0.5
    any_flag = {c for c, f in zip(s.neg_clusters, neg_flags, strict=True) if f}
    missed = {c for c, f in zip(s.pos_clusters, pos_flags, strict=True) if not f}
    assert t.flagged_neg == int(neg_flags.sum())
    assert t.flagged_pos == int(pos_flags.sum())
    assert t.fp_clusters == len(any_flag)
    assert t.detected_clusters == 15 - len(missed)
    _, upper = clopper_pearson(len(any_flag), 40, 0.05, side="upper")
    assert t.fpr_upper == pytest.approx(float(upper))
    fx = fixed_threshold(
        s.neg,
        s.pos,
        0.5,
        1e-3,
        "cluster_cp",
        neg_clusters=s.neg_clusters,
        pos_clusters=s.pos_clusters,
    )
    assert t.fpr == fx.fpr and t.recall == fx.recall
    assert [p for p, _ in t.precision] == list(CARD_PREVALENCES)
    assert t.precision[1][1] == fx.precision


def test_threshold_summary_validation() -> None:
    with pytest.raises(ValueError, match="finite"):
        threshold_summary(_clustered(), math.inf)
    with pytest.raises(ValueError, match="at least one prevalence"):
        threshold_summary(_clustered(), 0.5, prevalences=[])


def test_gap_with_false_positives_out_of_reach() -> None:
    gap = certification_gap(8, 1, 5)
    more = more_runs_fpr(1, 8, 0.0005)
    assert gap.more_if_none == more.if_no_events
    assert not gap.certified and not gap.reachable and gap.more_at_observed is None
    assert gap.sentence.startswith(
        f"{more.if_no_events:,} more independent benign runs with no false positive are "
        "needed to claim a false-positive rate of at most 0.0005 (5 per 10,000) at 95%"
    )
    assert "no number of added runs reaches it" in gap.sentence


def test_gap_reachable_at_observed_rate() -> None:
    gap = certification_gap(400, 1, 100)
    more = more_runs_fpr(1, 400, 0.01)
    assert not gap.certified and gap.reachable
    assert more.at_observed_rate is not None
    assert gap.more_at_observed == more.at_observed_rate
    assert f"about {more.at_observed_rate:,} more are needed" in gap.sentence


def test_gap_certified_and_unknown() -> None:
    assert zero_fp_runs(0.01) <= 400
    done = certification_gap(400, 0, 100)
    assert done.certified and done.more_if_none == 0
    assert done.sentence.startswith("These data already support a false-positive rate")
    guess = certification_gap(400, None, 100)
    assert guess.false_positive_runs is None
    assert "would support" in guess.sentence and "give a decision threshold" in guess.sentence
    short = certification_gap(10, None, 20)
    assert short.sentence.startswith("No decision threshold was given")
    assert f"{more_runs_fpr(0, 10, 0.002).if_no_events:,} more" in short.sentence


def test_card_with_threshold() -> None:
    s = _clustered()
    c = build_card(s, "  test monitor  ", threshold=0.5, reps=200, notes=["made-up data"])
    assert c.name == "test monitor"
    assert c.threshold is not None and c.threshold.threshold == 0.5
    assert len(c.certification) == len(CARD_BUDGETS)
    for gap, budget in zip(c.certification, CARD_BUDGETS, strict=True):
        expect = more_runs_fpr(c.threshold.fp_clusters, 40, flag_rate_from_budget(budget))
        assert gap.more_if_none == expect.if_no_events
        assert gap.current_bound == pytest.approx(expect.current_bound)
    md = render_markdown(c)
    for heading in (
        "# Monitor report card: test monitor",
        "## Data",
        "## Recall and precision at a review budget",
        "## Decision threshold: flag when score >= 0.5",
        "## Certification gap",
        "## Notes",
    ):
        assert heading in md
    assert "- made-up data" in md
    assert "Synthetic share: 0.500 (75 of 150 traces are marked synthetic)" in md
    assert md.count("| 0.0") >= 9  # the nine budget rows
    for gap in c.certification:
        assert gap.sentence in md
    assert EM_DASH not in md and EN_DASH not in md


def test_card_json_matches_card() -> None:
    s = _clustered()
    c = build_card(s, "m", threshold=0.5, reps=200)
    data = json.loads(render_json(c))
    assert data["schema"] == SCHEMA
    assert data["settings"]["threshold"] == 0.5
    assert data["settings"]["budgets_per_10k"] == list(CARD_BUDGETS)
    assert data["data"]["n_neg_clusters"] == 40
    assert len(data["budget_table"]) == 9
    first = data["budget_table"][0]
    assert first["recall"]["estimate"] == c.budget_rows[0].recall.estimate
    assert first["recall"]["lower"] == c.budget_rows[0].recall.lower
    assert c.threshold is not None
    assert data["decision_threshold"]["fp_clusters"] == c.threshold.fp_clusters
    assert [g["sentence"] for g in data["certification"]] == [g.sentence for g in c.certification]
    assert data["omitted"] == []
    text = render_json(c)
    assert EM_DASH not in text and EN_DASH not in text


def test_card_without_threshold() -> None:
    c = build_card(_clustered(synthetic=False), "m", reps=200)
    assert c.threshold is None
    assert all(g.false_positive_runs is None for g in c.certification)
    md = render_markdown(c)
    assert "## Decision threshold" not in md
    assert "No decision threshold was given" in md
    assert "Synthetic share: not stated" in md
    assert json.loads(render_json(c))["decision_threshold"] is None


def test_card_with_weights_omits_threshold_sections() -> None:
    c = build_card(_clustered(weights=True), "m", threshold=0.5, reps=200)
    assert c.threshold is None and c.certification == ()
    assert c.omitted == (WEIGHTS_OMITTED,)
    md = render_markdown(c)
    assert "## Certification gap" not in md
    assert "Sampling weights: present" in md
    assert WEIGHTS_OMITTED in md


def test_card_shows_stage2_count_comments_and_digest() -> None:
    base = _clustered()
    s2 = np.where(base.scores > 0.5, base.scores, np.nan)
    s = ScoreSet(
        trace_ids=base.trace_ids,
        labels=base.labels,
        scores=base.scores,
        clusters=base.clusters,
        stage2_scores=s2,
        source="x.csv",
        sha256="ab" * 32,
        comments=("from a comment line",),
    )
    md = render_markdown(build_card(s, "m", reps=100, budgets=[20], prevalences=[1e-3]))
    assert f"Stage-2 scores: {int(np.sum(~np.isnan(s2)))} of 150 traces." in md
    assert "From the score file: from a comment line" in md
    assert f"(SHA-256 `{'ab' * 32}`)" in md


def test_card_skips_gaps_for_budgets_at_or_above_all_traffic() -> None:
    c = build_card(_independent(50, 0), "m", threshold=0.5, budgets=[20, 10_000], reps=100)
    assert [g.budget_per_10k for g in c.certification] == [20]


def test_card_validation() -> None:
    s = _clustered()
    with pytest.raises(ValueError, match="non-empty name"):
        build_card(s, "  ")
    with pytest.raises(ValueError, match="alpha"):
        build_card(s, "m", alpha=1.5)
    with pytest.raises(ValueError, match="at least one budget"):
        build_card(s, "m", budgets=[])


def test_card_says_what_the_budget_method_counts() -> None:
    s = _clustered()
    default = render_markdown(build_card(s, "m", reps=50, budgets=[20], prevalences=[1e-3]))
    assert "clusters (source runs) are the independent units" in default
    plugin = build_card(s, "m", method="plugin_cp", budgets=[20], prevalences=[1e-3])
    md = render_markdown(plugin)
    assert "this method treats traces as independent" in md
    assert json.loads(render_json(plugin))["settings"]["budget_method_cluster_aware"] is False
    assert method_note("composite") != method_note("plugin_wilson")
    assert {
        "cluster_cp",
        "cluster_bootstrap",
        "bayesian_bootstrap",
        "composite",
    } == CLUSTER_AWARE_METHODS


def test_alpha_changes_the_confidence_text() -> None:
    c = build_card(_independent(400, 1), "m", threshold=0.5, alpha=0.1, reps=100)
    md = render_markdown(c)
    assert "at 90%" in md and "Intervals are 90%" in md
    assert c.certification[-1].alpha == 0.1


def test_formatting_helpers() -> None:
    assert fmt_prob(0.12345) == "0.123"
    assert fmt_small(0.000505) == "0.000505"
    assert fmt_small(0.0) == "0"
    iv = Interval(0.5, 0.25, 0.75, "x", 0.05)
    assert fmt_interval(iv) == "0.500 [0.250, 0.750]"
    assert fmt_interval(Interval(1e-4, 0.0, 2e-4, "x", 0.05), small=True) == "0.0001 [0, 0.0002]"
    assert confidence_text(0.05) == "95%"
    assert confidence_text(0.01) == "99%"
    assert e0_cell_text((), "composite") == "no warning"
    assert jsonable({"a": [1.0, math.nan, (math.inf, 2)]}) == {"a": [1.0, None, [None, 2]]}


def test_e0_source_is_the_packaged_one() -> None:
    text = resources.files("baserate.estimate").joinpath("unsupported_cells.json").read_text()
    assert e0_support_source() == json.loads(text)["source"]
    assert card_mod.e0_support_source() in render_markdown(
        build_card(_clustered(), "m", reps=50, budgets=[20], prevalences=[1e-3])
    )
