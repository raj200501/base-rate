"""Tests for the baserate command line: every command, exit codes, and output."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from baserate import cli, demo
from baserate.card import CARD_BUDGETS, CARD_PREVALENCES
from baserate.cascade import sweep_cascade
from baserate.certify import (
    fpr_runs_for_power,
    joint_plan,
    more_runs_fpr,
    more_runs_recall,
    zero_fp_runs,
    zero_miss_runs,
)
from baserate.estimate import flag_rate_from_budget, recall_at_budget, support
from baserate.scores import read_scores

ROOT = Path(__file__).resolve().parents[1]
MONITOR = str(ROOT / "examples" / "fixtures" / "monitor_scores.csv")
CASCADE = str(ROOT / "examples" / "fixtures" / "cascade_scores.csv")

Capture = pytest.CaptureFixture[str]


def run(argv: Sequence[str], capsys: Capture) -> tuple[int, str, str]:
    """Run the CLI; return (exit code, stdout, stderr), whether it returns or exits."""
    try:
        code = cli.main(list(argv))
    except SystemExit as exc:
        code = int(exc.code or 0)
    out = capsys.readouterr()
    return code, out.out, out.err


def write_csv(path: Path, rows: Sequence[str], header: str) -> str:
    path.write_text(header + "\n" + "\n".join(rows) + "\n")
    return str(path)


# ----------------------------------------------------------------- parser


def test_help_for_every_command(capsys: Capture) -> None:
    code, out, _ = run(["--help"], capsys)
    assert code == 0
    for name in ("eval", "certify", "cascade", "card", "demo", "budget"):
        assert name in out
    for name in ("eval", "certify", "cascade", "card"):
        code, out, _ = run([name, "--help"], capsys)
        assert code == 0
        assert f"usage: baserate {name}" in out


def test_missing_command_is_a_usage_error(capsys: Capture) -> None:
    code, _, err = run([], capsys)
    assert code == 2
    assert "required" in err


def test_build_parser_lists_commands() -> None:
    text = cli.build_parser().format_help()
    assert "eval" in text and "card" in text


def test_unknown_option_is_a_usage_error(capsys: Capture) -> None:
    code, _, err = run(
        ["eval", MONITOR, "--prevalence", "1e-3", "--budget", "20", "--bogus"], capsys
    )
    assert code == 2
    assert "unrecognized arguments: --bogus" in err


def test_demo_is_delegated(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake(argv: Sequence[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 7

    monkeypatch.setattr(demo, "main", fake)
    assert cli.main(["demo", "--out", "x.txt"]) == 7
    assert seen == [["--out", "x.txt"]]


# ------------------------------------------------------------------- eval


def test_eval_table(capsys: Capture) -> None:
    code, out, err = run(
        ["eval", MONITOR, "--prevalence", "1e-3", "1e-4", "--budget", "5", "20", "--reps", "200"],
        capsys,
    )
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0] == f"baserate eval: {MONITOR}"
    s = read_scores(MONITOR)
    assert (
        f"benign {s.n_neg} traces in {s.n_neg_clusters} independent clusters; violation "
        f"{s.n_pos} traces in {s.n_pos_clusters} independent clusters"
    ) in out
    header = next(i for i, line in enumerate(lines) if line.startswith("prevalence"))
    rows = lines[header + 1 :]
    assert len(rows) == 4
    assert rows[0].split()[:2] == ["0.001", "5"]
    assert f"{s.n_neg} ({s.n_neg_clusters})" in rows[0]
    assert "WARNING" not in out
    assert "E0 support data:" in out
    assert "clusters (source runs) are the independent units" in out


def test_eval_json_matches_library(capsys: Capture) -> None:
    code, out, _ = run(
        ["eval", MONITOR, "--prevalence", "1e-3", "--budget", "20", "--json", "--seed", "3"],
        capsys,
    )
    assert code == 0
    data = json.loads(out)
    assert data["schema"] == "baserate-eval/1"
    assert data["cluster_aware"] is True
    s = read_scores(MONITOR)
    est = recall_at_budget(
        s.neg,
        s.pos,
        1e-3,
        flag_rate_from_budget(20),
        neg_clusters=s.neg_clusters,
        pos_clusters=s.pos_clusters,
        rng=np.random.default_rng(3),
    )
    (row,) = data["rows"]
    assert row["recall"]["estimate"] == est.recall.estimate
    assert row["recall"]["lower"] == est.recall.lower
    assert row["precision"]["upper"] == est.precision.upper
    assert row["fpr"]["estimate"] == est.fpr.estimate
    assert row["n_neg_clusters"] == est.n_neg_clusters
    assert data["sha256"] == s.sha256


def test_eval_prints_e0_warning_beside_the_row(
    capsys: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    cells = (
        {
            "id": "cellZ",
            "estimator": "plugin_cp",
            "prevalence": 1e-3,
            "flag_rate": 0.002,
            "n_neg": 100,
            "n_pos": 100,
            "clustered": True,
        },
    )
    monkeypatch.setattr(support, "_cells", lambda: cells)
    code, out, _ = run(
        ["eval", MONITOR, "--prevalence", "1e-3", "--budget", "20", "--method", "plugin_cp"],
        capsys,
    )
    assert code == 0
    row = out.splitlines()[-1]
    assert row.startswith("0.001")
    assert "WARNING: plugin_cp under-covered in E0 cell cellZ" in row
    assert "this method treats traces as independent" in out


@pytest.mark.parametrize(
    "args",
    [
        ["--prevalence", "0", "--budget", "20"],
        ["--prevalence", "1", "--budget", "20"],
        ["--prevalence", "abc", "--budget", "20"],
        ["--prevalence", "nan", "--budget", "20"],
        ["--prevalence", "1e-3", "--budget", "0"],
        ["--prevalence", "1e-3", "--budget", "20000"],
        ["--prevalence", "1e-3"],
        ["--budget", "20"],
        ["--prevalence", "1e-3", "--budget", "20", "--alpha", "1"],
        ["--prevalence", "1e-3", "--budget", "20", "--reps", "0"],
        ["--prevalence", "1e-3", "--budget", "20", "--seed", "-1"],
        ["--prevalence", "1e-3", "--budget", "20", "--seed", "x"],
        ["--prevalence", "1e-3", "--budget", "20", "--method", "nope"],
        ["--prevalence", "1e-3", "--budget", "20", "--format", "xlsx"],
    ],
)
def test_eval_usage_errors(args: list[str], capsys: Capture) -> None:
    code, _, err = run(["eval", MONITOR, *args], capsys)
    assert code == 2
    assert "usage: baserate eval" in err


def test_eval_data_errors(tmp_path: Path, capsys: Capture) -> None:
    code, out, err = run(
        ["eval", str(tmp_path / "none.csv"), "--prevalence", "1e-3", "--budget", "20"], capsys
    )
    assert code == 1 and out == ""
    assert err.startswith("baserate eval: error: ") and "file not found" in err
    bad = write_csv(
        tmp_path / "bad.csv", ["a,benign,0.1,r", "b,unsure,0.2,r"], "trace_id,label,score,cluster"
    )
    code, _, err = run(["eval", bad, "--prevalence", "1e-3", "--budget", "20"], capsys)
    assert code == 1
    assert "bad.csv:3: label must be" in err


def test_eval_reads_jsonl_with_format_override(tmp_path: Path, capsys: Capture) -> None:
    rows = [
        json.dumps({"trace_id": f"t{i}", "label": lab, "score": sc, "cluster": f"r{i}"})
        for i, (lab, sc) in enumerate(
            [("benign", 0.1), ("benign", 0.2), ("benign", 0.4), ("violation", 0.9)]
        )
    ]
    path = tmp_path / "scores.txt"
    path.write_text("\n".join(rows) + "\n")
    code, out, err = run(
        ["eval", str(path), "--format", "jsonl", "--prevalence", "0.01", "--budget", "100"],
        capsys,
    )
    assert code == 0, err
    assert "benign 3 traces in 3 independent clusters" in out


def test_eval_notes_weights(tmp_path: Path, capsys: Capture) -> None:
    path = write_csv(
        tmp_path / "w.csv",
        ["a,benign,0.1,r1,2", "b,benign,0.2,r2,1", "c,violation,0.9,r3,1"],
        "trace_id,label,score,cluster,weight",
    )
    code, out, _ = run(["eval", path, "--prevalence", "0.01", "--budget", "100"], capsys)
    assert code == 0
    assert "sampling weights used" in out


# ---------------------------------------------------------------- certify


def test_certify_fpr_only(capsys: Capture) -> None:
    code, out, _ = run(["certify", "--fpr", "0.001"], capsys)
    assert code == 0
    assert f"zero-event: {zero_fp_runs(0.001):,} independent benign runs" in out
    plan = fpr_runs_for_power(0.001, 0.0005, 0.8)
    assert f"assuming a true FPR of 0.0005: {plan.runs:,} independent benign runs" in out
    assert "claim recall" not in out


def test_certify_json_matches_library(capsys: Capture) -> None:
    code, out, _ = run(
        [
            "certify",
            "--fpr",
            "0.001",
            "--recall",
            "0.9",
            "--have-runs",
            "500",
            "--have-violation-runs",
            "40",
            "--have-misses",
            "1",
            "--json",
        ],
        capsys,
    )
    assert code == 0
    data = json.loads(out)
    assert data["schema"] == "baserate-certify/1"
    assert data["fpr"]["zero_event_runs"] == zero_fp_runs(0.001)
    assert data["recall"]["zero_event_runs"] == zero_miss_runs(0.9)
    assert data["fpr"]["assumed_true_fpr"] == 0.0005
    assert data["recall"]["assumed_true_recall"] == 0.95
    more = more_runs_fpr(0, 500, 0.001, 0.05, 0.8)
    assert data["fpr"]["existing"]["if_no_events"] == more.if_no_events
    rec = more_runs_recall(39, 40, 0.9, 0.05, 0.8)
    assert data["recall"]["existing"]["if_no_events"] == rec.if_no_events
    assert data["recall"]["existing"]["for_power"] == rec.for_power
    joint = joint_plan(0.9, 0.001, 0.05, true_recall=0.95, true_fpr=0.0005, power=0.8)
    assert data["joint_power"]["n_neg"] == joint.n_neg
    assert data["joint_zero_event"]["n_pos"] == joint_plan(0.9, 0.001).n_pos


def test_certify_text_for_existing_claims(capsys: Capture) -> None:
    code, out, _ = run(
        [
            "certify",
            "--fpr",
            "0.001",
            "--recall",
            "0.9",
            "--true-fpr",
            "0.0002",
            "--true-recall",
            "0.97",
            "--have-runs",
            "5000",
            "--have-events",
            "9",
            "--have-violation-runs",
            "40",
        ],
        capsys,
    )
    assert code == 0
    more = more_runs_fpr(9, 5000, 0.001)
    assert f"{more.if_no_events:,} more benign runs with no false positive needed" in out
    assert "at the observed rate no number of added runs certifies FPR <= 0.001" in out
    assert "assuming a true FPR of 0.0002" in out
    assert "assuming a true recall of 0.97" in out
    assert more_runs_recall(40, 40, 0.9).certified
    assert "already certifies recall >= 0.9 at 95%" in out
    assert "both claims at an overall 95%" in out


def test_certify_already_certified_and_observed_rate(capsys: Capture) -> None:
    code, out, _ = run(
        ["certify", "--fpr", "0.01", "--have-runs", "1000", "--have-events", "1"], capsys
    )
    assert code == 0
    assert "already certifies FPR <= 0.01 at 95%" in out
    code, out, _ = run(
        ["certify", "--fpr", "0.01", "--have-runs", "300", "--have-events", "1"], capsys
    )
    more = more_runs_fpr(1, 300, 0.01, 0.05, 0.8)
    assert more.at_observed_rate is not None and more.for_power is not None
    assert f"{more.at_observed_rate:,} more if false positives continue" in out
    assert f"{more.for_power:,} more for power 0.8 at the observed rate" in out


def test_certify_power_not_reached(capsys: Capture) -> None:
    code, out, _ = run(["certify", "--fpr", "0.001", "--recall", "0.9", "--n-max", "100"], capsys)
    assert code == 0
    assert "not reached" in out
    assert "joint power 0.8 is not reached within 100 runs" in out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--have-events", "3"], "--have-events needs --have-runs"),
        (["--have-runs", "3", "--have-events", "4"], "cannot exceed --have-runs"),
        (["--true-fpr", "0.002"], "--true-fpr must be"),
        (["--true-recall", "0.95"], "--true-recall needs --recall"),
        (["--recall", "0.9", "--true-recall", "0.8"], "--true-recall must be above"),
        (["--have-misses", "1"], "need --recall"),
        (["--recall", "0.9", "--have-misses", "1"], "--have-misses needs --have-violation-runs"),
        (
            ["--recall", "0.9", "--have-violation-runs", "2", "--have-misses", "3"],
            "cannot exceed --have-violation-runs",
        ),
        (["--power", "1"], "between 0 and 1"),
        (["--true-fpr", "2"], "must be between 0 and 1, got 2"),
        (["--have-runs", "0"], "at least 1"),
    ],
)
def test_certify_usage_errors(args: list[str], message: str, capsys: Capture) -> None:
    code, _, err = run(["certify", "--fpr", "0.001", *args], capsys)
    assert code == 2
    assert message in err


def test_certify_requires_fpr(capsys: Capture) -> None:
    code, _, err = run(["certify"], capsys)
    assert code == 2 and "--fpr" in err


# ---------------------------------------------------------------- cascade


CASCADE_ARGS = ["--prevalence", "1e-3", "--budget", "20", "--c1", "1", "--c2", "10"]


def test_cascade_front_and_best(capsys: Capture) -> None:
    code, out, err = run(["cascade", CASCADE, *CASCADE_ARGS, "--max-cost", "3", "--all"], capsys)
    assert code == 0, err
    sweep = sweep_cascade(read_scores(CASCADE).two_stage(), 1e-3, 0.002, 1.0, 10.0)
    best = sweep.best_under(3.0)
    assert best is not None
    assert "Pareto front" in out and "all 25 cascades" in out
    front_rows = [line for line in out.splitlines() if line.startswith("  stage1")]
    assert front_rows, out
    assert f"best under cost 3: cascade escalating {best.escalation_rate:.4g} of traffic" in out
    assert f"recall {best.recall:.3f}" in out
    assert "without intervals" in out


def test_cascade_json(capsys: Capture) -> None:
    code, out, _ = run(["cascade", CASCADE, *CASCADE_ARGS, "--json", "--num-rates", "5"], capsys)
    assert code == 0
    data = json.loads(out)
    sweep = sweep_cascade(
        read_scores(CASCADE).two_stage(), 1e-3, 0.002, 1.0, 10.0, np.geomspace(0.002, 1, 5)
    )
    assert data["schema"] == "baserate-cascade/1"
    assert len(data["candidates"]) == 5
    assert [p["recall"] for p in data["front"]] == [p.recall for p in sweep.front]
    assert data["stage2"]["kind"] == "stage2"
    assert data["best_under_max_cost"] is None
    assert data["in_sample"] is True


def test_cascade_nothing_fits(capsys: Capture) -> None:
    code, out, _ = run(["cascade", CASCADE, *CASCADE_ARGS, "--max-cost", "0.5"], capsys)
    assert code == 0
    assert "no cascade or single monitor costs at most 0.5" in out


def test_cascade_baseline_wording(tmp_path: Path, capsys: Capture) -> None:
    path = write_csv(
        tmp_path / "c.csv",
        ["a,benign,0.1,r1,0.1", "b,benign,0.2,r2,0.3", "c,violation,0.9,r3,0.8"],
        "trace_id,label,score,cluster,stage2_score",
    )
    args = ["--prevalence", "0.01", "--budget", "100", "--c1", "1", "--c2", "2"]
    code, out, _ = run(["cascade", path, *args, "--max-cost", "1"], capsys)
    assert code == 0 and "best under cost 1: stage 1 alone" in out
    code, out, _ = run(
        [
            "cascade",
            path,
            "--prevalence",
            "0.01",
            "--budget",
            "100",
            "--c1",
            "5",
            "--c2",
            "1",
            "--max-cost",
            "5",
        ],
        capsys,
    )
    assert code == 0 and "stage 2 alone on all traffic" in out


def test_cascade_data_errors(tmp_path: Path, capsys: Capture) -> None:
    code, _, err = run(["cascade", MONITOR, *CASCADE_ARGS], capsys)
    assert code == 1 and "no stage2_score column" in err
    weighted = write_csv(
        tmp_path / "w.csv",
        ["a,benign,0.1,r1,0.1,1", "b,violation,0.9,r2,0.9,1"],
        "trace_id,label,score,cluster,stage2_score,weight",
    )
    code, _, err = run(["cascade", weighted, *CASCADE_ARGS], capsys)
    assert code == 1 and "does not take sampling weights" in err
    missing = write_csv(
        tmp_path / "m.csv",
        ["a,benign,0.1,r1,0.1", "b,benign,0.95,r2,", "c,violation,0.9,r3,0.9"],
        "trace_id,label,score,cluster,stage2_score",
    )
    code, _, err = run(["cascade", missing, *CASCADE_ARGS], capsys)
    assert code == 1 and "no stage-2 score" in err


def test_cascade_usage_errors(capsys: Capture) -> None:
    code, _, err = run(["cascade", CASCADE, "--prevalence", "1e-3", "--budget", "20"], capsys)
    assert code == 2 and "--c1" in err
    code, _, err = run(["cascade", CASCADE, *CASCADE_ARGS[:-1], "-3"], capsys)
    assert code == 2


# ------------------------------------------------------------------- card


def test_card_markdown(capsys: Capture) -> None:
    code, out, _ = run(
        ["card", MONITOR, "--name", "rules", "--threshold", "0.2", "--note", "demo data"],
        capsys,
    )
    assert code == 0
    assert out.startswith("# Monitor report card: rules\n")
    assert "## Decision threshold: flag when score >= 0.2" in out
    assert "- demo data" in out
    assert "more independent benign runs with no false positive are needed" in out
    rows = [line for line in out.splitlines() if line.startswith("| 0.0")]
    assert len(rows) == len(CARD_BUDGETS) * len(CARD_PREVALENCES)


def test_card_json_and_out(tmp_path: Path, capsys: Capture) -> None:
    target = tmp_path / "cards" / "card.json"
    code, out, _ = run(["card", MONITOR, "--name", "rules", "--json", "--out", str(target)], capsys)
    assert code == 0
    assert out.strip() == f"baserate card: wrote {target}"
    data = json.loads(target.read_text())
    assert data["name"] == "rules" and data["decision_threshold"] is None
    md = tmp_path / "card.md"
    code, _, _ = run(["card", MONITOR, "--name", "rules", "--out", str(md)], capsys)
    assert code == 0 and md.read_text().startswith("# Monitor report card: rules")


def test_card_errors(tmp_path: Path, capsys: Capture) -> None:
    code, _, err = run(["card", MONITOR, "--name", "  "], capsys)
    assert code == 2 and "--name must not be empty" in err
    code, _, err = run(["card", MONITOR], capsys)
    assert code == 2 and "--name" in err
    code, _, err = run(["card", MONITOR, "--name", "x", "--threshold", "inf"], capsys)
    assert code == 2
    code, _, err = run(["card", MONITOR, "--name", "x", "--out", str(tmp_path)], capsys)
    assert code == 1 and "cannot write" in err
    code, _, err = run(["card", str(tmp_path / "x.csv"), "--name", "x"], capsys)
    assert code == 1 and "file not found" in err
