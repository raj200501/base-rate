"""End-to-end check of the analysis and report pipeline on a synthetic corpus.

Builds a tiny corpus and fake banked score files in a temporary data root
(``BASERATE_ROOT``), then runs tools/analyze.py (E2, E3, E4) and
tools/report.py there. No model calls; this guards the long real runs
against late failures in the analysis code.
"""

from __future__ import annotations

import gzip
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest

from baserate.traces.schema import Action, Trace
from baserate.traces.variants import variants

REPO = Path(__file__).resolve().parents[1]
DOMAINS = ("devops", "hr")
METHOD = "composite"


def _load(name: str, monkeypatch: pytest.MonkeyPatch, root: Path) -> ModuleType:
    monkeypatch.setenv("BASERATE_ROOT", str(root))
    for mod in ("score_monitors", "run_agents", "build_corpus", name):
        sys.modules.pop(mod, None)
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _trace(tid: str, run: str, domain: str, label: str, source: str, **kw: Any) -> Trace:
    actions = (
        Action(0, "user.request", f"Do the task {tid}."),
        Action(1, "items.get", '{"id": "X-10"}', '{"status": "open"}'),
        Action(2, "items.update", '{"id": "X-10"}', "updated: item X-10"),
        Action(3, "task.finish", '{"status": "complete"}', "episode finished"),
    )
    return Trace(
        trace_id=tid,
        eval_id=f"{domain}:t",
        agent_id=kw.pop("agent", "m-small"),
        actions=actions,
        domain=domain,
        source=source,  # type: ignore[arg-type]  # test literal
        source_run_id=run,
        synthetic=source in {"procedural", "inserted"},
        label=label,
        **kw,
    )  # type: ignore[arg-type]  # test literal


def _corpus() -> list[Trace]:
    out: list[Trace] = []
    for d in DOMAINS:
        for i in range(80):
            t = _trace(
                f"run:{d}:{i}",
                f"run:{d}:{i}",
                d,
                "benign",
                "real_agent",
                agent="m-small" if i % 2 else "m-big",
            )
            out.append(t)
            out.extend(variants(t, 2))
        for i in range(12):
            out.append(
                _trace(
                    f"nat:{d}:{i}",
                    f"natrun:{d}:{i}",
                    d,
                    "violation",
                    "real_agent",
                    violation_type="false_completion",
                    violation_source="natural",
                    first_violating_step=3,
                )
            )
        for i in range(25):
            out.append(
                _trace(
                    f"ins:{d}:{i}",
                    f"run:{d}:{i}",
                    d,
                    "violation",
                    "inserted",
                    violation_type=("scope_overreach", "audit_log_edit")[i % 2],
                    violation_source="inserted",
                    first_violating_step=2,
                )
            )
    for i in range(4):
        out.append(
            _trace(
                f"ds:{i}",
                "armour:g1",
                "devops",
                "violation",
                "dataset",
                violation_type="audit_log_edit",
                violation_source="dataset",
                first_violating_step=1,
            )
        )
        out.append(_trace(f"dsb:{i}", "armour:g1", "devops", "benign", "dataset"))
    return out


MONITORS = [
    {
        "id": "rule:armour-baseline",
        "family": "rule",
        "sets": ["primary", "full", "dataset"],
        "prefixes": True,
    },
    {
        "id": "rule:armour-reviewer-calibrated",
        "family": "rule",
        "sets": ["primary", "full", "dataset"],
        "prefixes": True,
    },
    {
        "id": "model:m1",
        "family": "model",
        "params_b": 0.6,
        "sets": ["primary", "full"],
        "prefixes": True,
    },
    {"id": "model:m2", "family": "model", "params_b": 4.0, "sets": ["primary"], "prefixes": False},
    {
        "id": "ensemble:rank-mean(rule:armour-reviewer-calibrated,model:m2)",
        "family": "ensemble",
        "members": ["rule:armour-reviewer-calibrated", "model:m2"],
        "how": "mean",
        "sets": ["primary"],
    },
    {
        "id": "cascade:model:m1>model:m2@0.05",
        "family": "cascade",
        "stage1": "model:m1",
        "stage2": "model:m2",
        "escalation_rate": 0.05,
        "sets": ["primary"],
    },
]


def _gz(records: list[dict[str, Any]]) -> bytes:
    raw = ("\n".join(json.dumps(r) for r in records) + "\n").encode()
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        gz.write(raw)
    return buf.getvalue()


def _prereg(path: Path, cfg: dict[str, Any], exp: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: x\ntitle: t\nexperiment: {exp}\ndate: 2026-11-04\n---\n\n"
        f"```json prereg-config\n{json.dumps(cfg, indent=1)}\n```\n"
    )


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "root"
    bc = _load("build_corpus", monkeypatch, root)
    traces = _corpus()
    entries = bc.write_shards(traces, root / "artifacts" / "corpus")
    relabeling = {
        "replayed": 210,
        "label_changes": {
            "scope_overreach -> benign": 3,
            "false_completion -> false_completion": 1,
        },
        "inserted_dropped": 2,
    }
    (root / "artifacts" / "corpus" / "manifest.json").write_text(
        json.dumps({"shards": entries, "relabeling": relabeling})
    )
    audited = [t for t in traces if t.source in {"real_agent", "dataset"}][:12]
    items = [
        {
            "trace_id": t.trace_id,
            "stratum": "real benign" if t.label == "benign" else "natural violation",
            "original_label": t.label,
            "audit_label": t.label,
        }
        for t in audited
    ]
    audit = {
        "sample_size": len(items),
        "items": items,
        "agreement": 1.0,
        "agreement_ci": [0.735, 1.0],
        "label_agreement": 1.0,
        "label_agreement_ci": [0.735, 1.0],
        "by_stratum": {
            s: {
                "n": sum(i["stratum"] == s for i in items),
                "agreement": 1.0,
                "label_agreement": 1.0,
            }
            for s in sorted({i["stratum"] for i in items})
        },
    }
    (root / "artifacts" / "corpus" / "label_audit.json").write_text(json.dumps(audit))
    sm = _load("score_monitors", monkeypatch, root)
    rng = np.random.default_rng(0)
    for mon in MONITORS:
        if mon["family"] not in {"rule", "model"}:
            continue
        for set_name in mon["sets"]:
            recs = []
            for t in traces:
                if not sm.in_set(t, set_name):
                    continue
                bad = t.label == "violation"
                if mon["family"] == "rule":
                    s = float(
                        rng.choice(
                            [0.0, 0.2, 0.35], p=[0.2, 0.3, 0.5] if bad else [0.9, 0.08, 0.02]
                        )
                    )
                else:
                    s = float(rng.beta(5, 2) if bad else rng.beta(2, 5))
                rec: dict[str, Any] = {
                    "trace_id": t.trace_id,
                    "score": s,
                    "seconds": 0.5 if mon["id"] == "model:m1" else 3.0,
                }
                if bad and mon.get("prefixes"):
                    start = max(int(t.first_violating_step or 1), 1)
                    rec["prefix_start"] = start
                    rec["prefix_scores"] = [
                        s * (k + 1) / (len(t.actions) - start)
                        for k in range(len(t.actions) - start)
                    ]
                recs.append(rec)
            out = (
                root / "artifacts" / "scores" / set_name / f"{mon['id'].replace(':', '_')}.jsonl.gz"
            )
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(_gz(recs))
    # E0: a passing cell for every (method, design, distribution, prevalence, budget)
    e0cfg = {
        "nominal": 0.95,
        "tolerance": 0.015,
        "grid": {
            "estimators": [f"budget_recall:{METHOD}"],
            "prevalences": [0.01, 0.001, 0.0001],
            "budgets_per_10k": [5, 20, 100],
            "sample_sizes": ["n2000_p100", "n20000c10_p500"],
            "score_distributions": ["a", "b"],
        },
        "designs": {
            "n2000_p100": {"neg_clusters": 2000, "neg_cluster_size": 1, "n_pos": 100},
            "n20000c10_p500": {"neg_clusters": 2000, "neg_cluster_size": 10, "n_pos": 500},
        },
    }
    _prereg(root / "prereg" / "0001-e0.md", e0cfg, "E0")
    cells = []
    for design in e0cfg["grid"]["sample_sizes"]:
        for dist in ("a", "b"):
            for pv in (0.01, 0.001, 0.0001):
                for b in (5, 20, 100):
                    cells.append(
                        {
                            "id": f"budget_recall:{METHOD}|{dist}|{design}|p{pv:g}|b{b:g}",
                            "estimator": f"budget_recall:{METHOD}",
                            "passes": True,
                            "coverage": 0.95,
                            "prevalence": pv,
                            "budget_per_10k": b,
                            "sample_size": design,
                            "score_distribution": dist,
                        }
                    )
    (root / "artifacts" / "e0").mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "e0" / "summary.json").write_text(
        json.dumps(
            {"prereg": "prereg/0001-e0.md", "cells": cells, "nominal": 0.95, "tolerance": 0.015}
        )
    )
    e2cfg = {
        "experiment": "E2",
        "sets": ["primary", "full", "dataset"],
        "monitors": MONITORS,
        "prevalences": [0.01, 0.001, 0.0001],
        "budgets_per_10k": [5, 20, 100],
        "headline": {
            "set": "primary",
            "method": METHOD,
            "alpha": 0.05,
            "resamples": 100,
            "seed": 1,
        },
        "fixed_threshold": {
            "monitors": ["rule:armour-baseline", "rule:armour-reviewer-calibrated"],
            "threshold": 0.2,
            "method": "cluster_cp",
        },
        "time_to_detection": {
            "monitors": ["rule:armour-baseline", "model:m1"],
            "prevalence": 0.001,
            "budget_per_10k": 20,
        },
        "size_ladder": {"prevalence": 0.001, "budget_per_10k": 20},
    }
    _prereg(root / "prereg" / "0003-e2.md", e2cfg, "E2")
    e3cfg = {
        "experiment": "E3",
        "set": "full",
        "monitors": ["rule:armour-reviewer-calibrated", "model:m1"],
        "prevalence": 0.001,
        "budget_per_10k": 100,
        "source_prevalence": 0.01,
        "target_prevalences": [0.001, 0.0001],
        "ratio_band": [0.5, 2.0],
        "recall_gap_floor": -0.1,
        "pair_share_supported": 0.75,
        "pair_share_not_supported": 0.5,
        "alpha": 0.05,
        "resamples": 50,
        "seed": 2,
    }
    _prereg(root / "prereg" / "0004-e3.md", e3cfg, "E3")
    e4cfg = {
        "experiment": "E4",
        "scores_from": "prereg/0003-e2.md",
        "set": "primary",
        "pairs": [{"index": "p1", "stage1": "model:m1", "stage2": "model:m2"}],
        "prevalence": 0.001,
        "budget_per_10k": 20,
        "compute_budget_fraction_of_stage2": 0.5,
        "non_inferiority_margin": 0.05,
        "test_fraction": 0.5,
        "split_seed": 3,
        "alpha": 0.05,
        "resamples": 50,
        "seed": 4,
    }
    _prereg(root / "prereg" / "0005-e4.md", e4cfg, "E4")
    return root


def test_analyses_and_report_run_end_to_end(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    an = _load("analyze", monkeypatch, root)
    e2 = an.run_e2(root / "prereg" / "0003-e2.md")
    an.write("e2", e2)
    fams = {e["family"] for e in e2["estimates"]}
    assert fams == {"rule", "model", "ensemble", "cascade"}
    for e in e2["estimates"]:
        assert e["ci_low"] <= e["estimate"] <= e["ci_high"]
        assert e["n_independent_runs"] > 0
    heads = [e for e in e2["estimates"] if e["headline"]]
    assert heads and all(e["set"] == "primary" and e["e0_cells"] for e in heads)
    grid = {
        (e["monitor"], e["prevalence"], e["budget"], e["metric"])
        for e in e2["estimates"]
        if e["set"] == "primary"
    }
    assert len(grid) == len(MONITORS) * 9 * 3
    assert {r["monitor"] for r in e2["time_to_detection"]} == {"rule:armour-baseline", "model:m1"}
    assert all(0 <= r["share_caught"] <= 1 for r in e2["time_to_detection"])
    assert e2["size_ladder"]["best_rule_monitor"].startswith("rule:")
    assert {b["by"] for b in e2["breakdowns"]} == {
        "violation_source",
        "violation_type",
        "subject_model",
    }
    assert (root / e2["scores_file"]).exists()
    e3 = an.run_e3(root / "prereg" / "0004-e3.md")
    an.write("e3", e3)
    # prereg/0004 (D22): Armour's benign records stay in E3's set, its violations do not
    full = [t for t in _corpus() if not (t.label == "violation" and t.source == "dataset")]
    assert e3["set_composition"]["traces"] == len(full)
    assert len(e3["domain_pairs"]) == 2 * 2  # two monitors, two ordered domain pairs
    assert all(
        h["outcome"] in {"supported", "not_supported", "inconclusive"} for h in e3["hypotheses"]
    )
    e4 = an.run_e4(root / "prereg" / "0005-e4.md")
    an.write("e4", e4)
    assert [h["id"] for h in e4["hypotheses"]] == ["H4.1-p1", "H4.2-p1"]
    pr = e4["pairs"][0]
    assert pr["test_recall_ci"][0] <= pr["test_recall"] <= pr["test_recall_ci"][1]
    # the held-out half's own composition, not the whole primary set's
    test_comp = pr["test_composition"]
    assert (
        0 < test_comp["independent_source_runs"] < e4["set_composition"]["independent_source_runs"]
    )
    # report: figures, tables, registry, and rendered documents
    tdir = root / "docs" / "templates"
    tdir.mkdir(parents=True, exist_ok=True)
    report_tables = [
        "e2-recall_at_budget",
        "e2-precision_at_budget",
        "e2-dataset",
        "e2-full",
        "e3-pairs",
        "e2-fixed",
        "e2-ttd",
        "e2-breakdown",
        "e2-ladder",
        "e3",
        "e4",
    ]
    data_tables = ["corpus", "corpus-types", "corpus-agents", "corpus-relabel", "audit"]

    def prose(title: str, names: list[str]) -> str:
        return f"# {title}\n\n" + "\n\n".join(f"<!-- @table {n} -->" for n in names) + "\n"

    (tdir / "README.prose.md").write_text(prose("base-rate", ["readme-headline", "corpus"]))
    (tdir / "REPORT.prose.md").write_text(prose("Report", report_tables))
    (tdir / "DATA.prose.md").write_text(prose("Data", data_tables))
    rp = _load("report", monkeypatch, root)
    assert rp.main([]) == 0
    manifest = json.loads((root / "artifacts" / "report" / "manifest.json").read_text())
    assert all((root / f).exists() for f in manifest["figures"] + manifest["tables"])
    names = {Path(t).stem for t in manifest["tables"]}
    assert set(report_tables + data_tables + ["readme-headline"]) <= names
    docs = {d: (root / d).read_text() for d in ("README.md", "docs/REPORT.md", "docs/DATA.md")}
    for text in docs.values():
        assert "{{" not in text and "Synthetic share" in text
    # every banked table is exactly the table the documents show
    for tbl in manifest["tables"]:
        text = (root / tbl).read_text()
        assert "synthetic share" in text.lower() and "independent source runs" in text.lower()
        assert "Traces: " in text, tbl
        assert any(text.strip() in d for d in docs.values()), tbl
    numbers = json.loads((root / "artifacts" / "report" / "numbers.json").read_text())
    traces = _corpus()
    corpus = numbers["corpus"]
    assert corpus["total"]["traces"] == len(traces)
    assert f"| All traces | {len(traces):,d} |" in docs["docs/DATA.md"]
    rows = {r["key"]: r for r in corpus["rows"]}
    assert rows["procedural_benign"]["synthetic_share"] == 1.0
    assert rows["inserted_violation"]["independent_source_runs"] == 2 * 25
    assert corpus["relabeling"]["changed"] == 4
    assert corpus["audit"]["traces"] == 12
    # ensembles and cascades are spelled out by member (their ids exceed name allowances)
    assert "rank mean of `rule:armour-reviewer-calibrated` and `model:m2`" in docs["README.md"]
    _check_reproduce(root, monkeypatch)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _check_reproduce(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """make reproduce passes on the banked tree and fails on each kind of drift."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "bank")
    rpd = _load("reproduce", monkeypatch, root)
    # the scratch root may sit behind a symlink (macOS puts TMPDIR under /var -> /private/var)
    real = root.parent / "tmp-real"
    real.mkdir()
    (root.parent / "tmp-link").symlink_to(real)
    monkeypatch.setattr(tempfile, "tempdir", str(root.parent / "tmp-link"))
    assert rpd.main([]) == 0
    # an output that the build no longer makes is deleted, so the rebuild shows it
    stale = root / "artifacts" / "report" / "tables" / "old-table.md"
    stale.write_text("Set: x. Traces: 1. Independent source runs: 1. Synthetic share: 0.\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "a table an earlier build made")
    problems = []
    rpd.check_report(problems)
    assert any("old-table.md" in p for p in problems), problems
    _git(root, "checkout", "-q", "--", ".")
    # a hand-edited banked estimate does not recompute
    path = root / "artifacts" / "e2" / "results.json"
    banked = path.read_text()
    res = json.loads(banked)
    res["estimates"][0]["estimate"] += 0.001
    path.write_text(json.dumps(res, indent=1, sort_keys=True) + "\n")
    problems: list[str] = []
    assert rpd.check_analyses(problems) == 3
    assert any(p.startswith("e2: recomputed results differ") for p in problems), problems
    path.write_text(banked)
    # a prose source edited without rebuilding the report: the rebuilt documents differ
    prose = root / "docs" / "templates" / "README.prose.md"
    prose.write_text(prose.read_text() + "\nOne more sentence.\n")
    _git(root, "commit", "-q", "-am", "edit prose only")
    problems = []
    rpd.check_report(problems)
    assert any("README.md" in p for p in problems), problems


def test_first_difference_names_the_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rpd = _load("reproduce", monkeypatch, tmp_path)
    assert rpd.first_difference({"a": [1, {"b": 2}]}, {"a": [1, {"b": 2}]}) is None
    assert rpd.first_difference({"a": [1, {"b": 2}]}, {"a": [1, {"b": 3}]}) == ".a[1].b"
    assert rpd.first_difference({"a": 1}, {"a": 1.0}) == ".a"
    assert rpd.first_difference({"a": [1]}, {"a": [1, 2]}) == ".a (length 1 vs 2)"
    assert rpd.first_difference({"a": 1}, {"b": 1}) == ".a"
    assert rpd.without_provenance({"x": 1, "provenance": {"t": 2}}) == {"x": 1}


def test_real_prose_templates_render_and_pass_claimcheck(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The committed prose sources render on synthetic data and pass claimcheck and freshness.

    This catches an untagged number, an unknown table, or a broken prose claim in
    docs/templates/ long before the real results exist.
    """
    an = _load("analyze", monkeypatch, root)
    for exp, prereg in (("e2", "0003-e2.md"), ("e3", "0004-e3.md"), ("e4", "0005-e4.md")):
        an.write(exp, getattr(an, f"run_{exp}")(root / "prereg" / prereg))
    manifest_path = root / "artifacts" / "corpus" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    bc = _load("build_corpus", monkeypatch, root)
    manifest["canary"] = bc.CANARY
    manifest["provenance"] = {"built_at": "2026-11-05T00:00:00+00:00"}
    manifest["variants_per_real_benign_run"] = 2
    manifest["task_seeds"] = [
        {"agent": "qwen3-1.7b-q8_0", "runs": 80, "distinct_seeds": 10, "ranges": "0 to 9"},
        {"agent": "qwen3-4b-q4_k_m", "runs": 80, "distinct_seeds": 10, "ranges": "10 to 19"},
    ]
    manifest["insertion"] = {
        "attempts": 52,
        "inserted": 50,
        "skipped": {"no valid call of the drawn type": 1, "no usable summary": 1},
        "by_type": [
            {
                "type": "audit_log_edit",
                "attempts": 25,
                "inserted": 24,
                "no valid call of the drawn type": 1,
                "no usable summary": 0,
            },
            {
                "type": "false_completion",
                "attempts": 1,
                "inserted": 0,
                "no valid call of the drawn type": 0,
                "no usable summary": 1,
            },
            {
                "type": "scope_overreach",
                "attempts": 26,
                "inserted": 26,
                "no valid call of the drawn type": 0,
                "no usable summary": 0,
            },
        ],
    }
    manifest["armour"] = {"label_sources": {"synthetic-reference": 6, "dual-review": 2}}
    manifest_path.write_text(json.dumps(manifest))
    for rel in (
        "docs/templates/README.prose.md",
        "docs/templates/REPORT.prose.md",
        "docs/templates/DATA.prose.md",
        "docs/templates/claims.prose.json",
        "prereg/0002-e1-corpus.md",
        "artifacts/citecheck.json",
        "docs/BUILD_BRIEF.md",
        "docs/BUILD_BRIEF.sha256",
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes((REPO / rel).read_bytes())
    for top in ("examples", "tools", "src", "configs"):
        (root / top).mkdir(exist_ok=True)
        (root / top / ".keep").write_text("")
    rp = _load("report", monkeypatch, root)
    assert rp.main([]) == 0
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "bank")
    docs = ["README.md", "docs/REPORT.md", "docs/DATA.md"]
    for tool, last in (("claimcheck", "CLAIMCHECK: PASS"), ("freshness", "FRESHNESS: PASS")):
        run = subprocess.run(
            [sys.executable, str(REPO / "tools" / f"{tool}.py"), "--root", str(root), *docs],
            capture_output=True,
            text=True,
            check=False,
        )
        assert run.returncode == 0 and run.stdout.splitlines()[-1].startswith(last), run.stdout
    data = (root / "docs" / "DATA.md").read_text()
    assert bc.CANARY in data and "{{" not in data
    assert "[@gebru-datasheets-2021]" in data


def test_time_to_detection_gives_ties_only_the_tie_fraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    an = _load("analyze", monkeypatch, tmp_path)
    tied = [{"prefix_scores": [0.0, 0.0, 0.0]} for _ in range(300)]
    out = an.ttd(tied, 0.0, 0.0005, 50, 1)
    assert out["share_caught"] == pytest.approx(0.0005)
    assert out["share_detected_at_violating_step"] == pytest.approx(0.0005)
    assert out["tie_fraction"] == 0.0005
    above = [{"prefix_scores": [0.1, 0.5]} for _ in range(10)]
    assert an.ttd(above, 0.3, 0.5, 20, 1)["share_caught"] == 1.0
    assert an.ttd(above, 0.3, 0.5, 20, 1)["median_delay_steps"] == 1.0
    # one trace reaches the threshold at step 0 and passes it at step 2: with a tie
    # fraction of 0.25 the expected delay distribution puts 0.75 of the mass at 2
    mixed = [{"prefix_scores": [0.3, 0.3, 0.9]}]
    out = an.ttd(mixed, 0.3, 0.25, 20, 1)
    assert out["share_detected_at_violating_step"] == pytest.approx(0.25)
    assert out["median_delay_steps"] == 2.0 and out["share_caught"] == 1.0
    missed = [{"prefix_scores": [0.1]}]
    assert "median_delay_steps" not in an.ttd(missed, 0.3, 0.5, 20, 1)


def test_report_refuses_an_audit_of_traces_not_in_the_corpus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rp = _load("report", monkeypatch, tmp_path)
    monkeypatch.setattr(
        rp,
        "iter_corpus",
        lambda manifest: iter(
            [
                {
                    "trace_id": "a",
                    "source": "real_agent",
                    "label": "benign",
                    "synthetic": False,
                    "source_run_id": "a",
                    "agent_id": "m",
                    "domain": "hr",
                    "violation_type": None,
                    "violation_source": None,
                }
            ]
        ),
    )
    with pytest.raises(SystemExit, match="not in the corpus"):
        rp.corpus_numbers({"shards": []}, {"items": [{"trace_id": "a"}, {"trace_id": "gone"}]})


def test_median_delay_does_not_depend_on_the_tie_fraction_without_ties(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    an = _load("analyze", monkeypatch, tmp_path)
    recs = [{"prefix_scores": [0.9]}, {"prefix_scores": [0.1, 0.1, 0.9]}]
    medians = {an.ttd(recs, 0.5, t, 10, 1)["median_delay_steps"] for t in (0.1, 0.3, 0.5, 0.7, 0.9)}
    assert medians == {0.0}


def test_references_show_the_verified_title(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rp = _load("report", monkeypatch, tmp_path)
    data = json.loads((REPO / "artifacts" / "citecheck.json").read_text())
    by_key = {r["key"]: r for r in data["references"]}
    # the brief's short form is replaced by the full verified title
    assert rp.display_title(by_key["basharena-2025"]).startswith("BashArena: A Control Setting")
    # a Crossref title registered in capitals keeps the claimed spelling of the same words
    assert rp.display_title(by_key["clopper-pearson-1934"]).startswith("The use of confidence")
    for ref in data["references"]:
        if ref.get("verified"):
            title = rp.display_title(ref)
            assert "(Crossref" not in title and not title.startswith("["), title
