"""Tests for baserate.scores: reading, validating, and writing score files."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from baserate.scores import (
    LABELS,
    OPTIONAL_COLUMNS,
    REQUIRED_COLUMNS,
    ScoreFileError,
    ScoreSet,
    dumps_scores,
    format_of,
    parse_csv,
    parse_jsonl,
    read_scores,
    write_scores,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples" / "fixtures" / "monitor_scores.csv"

GOOD_CSV = """\
# made-up rows for a format test
trace_id,label,score,cluster,stage2_score,weight,synthetic,notes
a,benign,0.1,run1,0.2,1.5,true,x
b,benign,0.3,run1,,1.0,false,y
c,violation,0.9,run2,0.95,2.0,yes,z
d,violation,0.7,run1,0.6,0.5,0,w
"""


def _full() -> ScoreSet:
    return parse_csv(GOOD_CSV, "good.csv")


def test_parse_csv_reads_every_column() -> None:
    s = _full()
    assert s.trace_ids == ("a", "b", "c", "d")
    assert s.labels == ("benign", "benign", "violation", "violation")
    assert s.scores.tolist() == [0.1, 0.3, 0.9, 0.7]
    assert s.clusters == ("run1", "run1", "run2", "run1")
    assert s.stage2_scores is not None and math.isnan(s.stage2_scores[1])
    assert s.weights is not None and s.weights.tolist() == [1.5, 1.0, 2.0, 0.5]
    assert s.synthetic is not None and s.synthetic.tolist() == [True, False, True, False]
    assert s.comments == ("made-up rows for a format test",)
    assert s.ignored_columns == ("notes",)
    assert s.columns == REQUIRED_COLUMNS + OPTIONAL_COLUMNS


def test_class_views_and_counts() -> None:
    s = _full()
    assert (s.n_traces, s.n_neg, s.n_pos) == (4, 2, 2)
    assert s.neg.tolist() == [0.1, 0.3]
    assert s.pos.tolist() == [0.9, 0.7]
    assert s.neg_clusters.tolist() == ["run1", "run1"]
    assert s.pos_clusters.tolist() == ["run2", "run1"]
    assert (s.n_clusters, s.n_neg_clusters, s.n_pos_clusters) == (2, 1, 2)
    assert s.neg_weights is not None and s.neg_weights.tolist() == [1.5, 1.0]
    assert s.pos_weights is not None and s.pos_weights.tolist() == [2.0, 0.5]
    assert s.n_synthetic == 2
    assert s.synthetic_share == pytest.approx(0.5)
    assert s.has_stage2
    assert s.is_violation.tolist() == [False, False, True, True]


def test_arrays_are_read_only() -> None:
    s = _full()
    with pytest.raises(ValueError):
        s.scores[0] = 5.0


def test_minimal_columns_have_no_optional_data() -> None:
    s = parse_csv("trace_id,label,score,cluster\na,benign,0,r\nb,violation,1,r\n")
    assert s.stage2_scores is None and s.weights is None and s.synthetic is None
    assert s.neg_weights is None and s.pos_weights is None
    assert s.synthetic_share is None and s.n_synthetic is None
    assert not s.has_stage2
    assert s.columns == REQUIRED_COLUMNS
    with pytest.raises(ScoreFileError, match="no stage2_score column"):
        s.two_stage()


def test_header_is_normalized_and_whitespace_trimmed() -> None:
    text = " Trace_ID , LABEL ,Score,Cluster\n a , Benign , 0.5 , r1 \nb,VIOLATION,1e-3,r2\n"
    s = parse_csv(text)
    assert s.trace_ids == ("a", "b")
    assert s.labels == ("benign", "violation")
    assert s.scores.tolist() == [0.5, 0.001]
    assert s.clusters == ("r1", "r2")


def test_blank_lines_crlf_and_bom(tmp_path: Path) -> None:
    text = "\r\ntrace_id,label,score,cluster\r\n\r\na,benign,0.1,r\r\n,,,\r\nb,violation,0.2,r\r\n"
    path = tmp_path / "s.csv"
    path.write_bytes(b"\xef\xbb\xbf" + text.encode())
    s = read_scores(path)
    assert s.trace_ids == ("a", "b")


def test_two_stage_keeps_alignment_and_clusters() -> None:
    two = _full().two_stage()
    assert two.s1_neg.tolist() == [0.1, 0.3]
    assert math.isnan(float(two.s2_neg[1]))
    assert two.s2_pos.tolist() == [0.95, 0.6]
    assert two.neg_clusters is not None and two.neg_clusters.tolist() == ["run1", "run1"]


@pytest.mark.parametrize("fmt", ["csv", "jsonl"])
def test_round_trip(tmp_path: Path, fmt: str) -> None:
    s = _full()
    path = tmp_path / f"scores.{fmt}"
    write_scores(s, path, comments=["first line", "second\nline"])
    back = read_scores(path)
    assert back.trace_ids == s.trace_ids
    assert back.labels == s.labels
    assert back.clusters == s.clusters
    np.testing.assert_array_equal(back.scores, s.scores)
    assert back.stage2_scores is not None and s.stage2_scores is not None
    np.testing.assert_array_equal(back.stage2_scores, s.stage2_scores)
    assert back.weights is not None and s.weights is not None
    np.testing.assert_array_equal(back.weights, s.weights)
    assert back.synthetic is not None and s.synthetic is not None
    np.testing.assert_array_equal(back.synthetic, s.synthetic)
    assert back.comments == ("first line", "second", "line")
    assert back.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert back.source == str(path)


def test_floats_round_trip_exactly() -> None:
    values = np.array([0.1 + 0.2, 1 / 3, 1e-300, -2.5, 123456789.123456789])
    s = ScoreSet(
        trace_ids=tuple("abcde"),
        labels=("benign", "benign", "violation", "benign", "violation"),
        scores=values,
        clusters=tuple("abcde"),
    )
    for fmt, parse in (("csv", parse_csv), ("jsonl", parse_jsonl)):
        back = parse(dumps_scores(s, fmt))
        np.testing.assert_array_equal(back.scores, values)


def test_dumps_is_deterministic_and_marks_missing_stage2() -> None:
    s = _full()
    assert dumps_scores(s, "csv") == dumps_scores(s, "csv")
    csv_text = dumps_scores(s, "csv")
    assert csv_text.splitlines()[0] == "trace_id,label,score,cluster,stage2_score,weight,synthetic"
    assert "b,benign,0.3,run1,,1.0,false" in csv_text
    assert '"stage2_score": null' in dumps_scores(s, "jsonl")
    assert dumps_scores(s, "csv", ["note"]).startswith("# note\n")


def test_fixture_reads_with_provenance_comments() -> None:
    s = read_scores(FIXTURE)
    assert s.sha256 == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert s.n_neg > 0 and s.n_pos > 0
    assert any("not study results" in c for c in s.comments)
    assert s.synthetic is not None


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("", "empty file: no header row"),
        ("# only a comment\n", "empty file: no header row"),
        ("trace_id,label,score,cluster\n", "no data rows"),
        ("trace_id,label,score\na,benign,0.1\n", "missing required column.*cluster"),
        ("trace_id;label;score;cluster\n", "';' as the separator"),
        ("trace_id,label,score,cluster,label\n", "'label' appears more than once"),
        ("trace_id,,score,cluster\n", "column 2 of the header has no name"),
        ("trace_id,label,score,cluster\na,safe,0.1,r\n", r":2: label must be .*'safe'"),
        ("trace_id,label,score,cluster\na,benign,abc,r\n", r":2: score is not a number"),
        ("trace_id,label,score,cluster\na,benign,,r\n", r":2: score is empty"),
        ("trace_id,label,score,cluster\na,benign,nan,r\n", r":2: score must be finite"),
        ("trace_id,label,score,cluster\na,benign,inf,r\n", r":2: score must be finite"),
        ("trace_id,label,score,cluster\na,benign,0.1,\n", r":2: cluster is empty"),
        ("trace_id,label,score,cluster\n,benign,0.1,r\n", r":2: trace_id is empty"),
        (
            "trace_id,label,score,cluster\na,benign,0.1,r\na,violation,0.2,r\n",
            r":3: trace_id 'a' also appears on line 2",
        ),
        ("trace_id,label,score,cluster\na,benign,0.1,r\n", "no violation traces"),
        ("trace_id,label,score,cluster\na,violation,0.1,r\n", "no benign traces"),
        ("trace_id,label,score,cluster\na,benign,0.1,r,extra\n", r":2: 5 values for 4 columns"),
        ("trace_id,label,score,cluster\na,benign,0.1\n", r":2: 3 values for 4 columns"),
        (
            "trace_id,label,score,cluster,weight\na,benign,0.1,r,0\n",
            r":2: weight must be positive",
        ),
        ("trace_id,label,score,cluster,weight\na,benign,0.1,r,\n", r":2: weight is empty"),
        (
            "trace_id,label,score,cluster,synthetic\na,benign,0.1,r,maybe\n",
            r":2: synthetic must be true or false",
        ),
        (
            "trace_id,label,score,cluster,stage2_score\na,benign,0.1,r,nan\n",
            r":2: stage2_score must be finite.*leave it empty",
        ),
        (
            "trace_id,label,score,cluster,stage2_score\na,benign,0.1,r,x\n",
            r":2: stage2_score is not a number.*leave it empty",
        ),
        ('trace_id,label,score,cluster\n"a,benign,0.1,r\n', "malformed CSV|values for"),
    ],
)
def test_csv_errors(text: str, match: str) -> None:
    with pytest.raises(ScoreFileError, match=match):
        parse_csv(text, "bad.csv")


def test_malformed_csv_is_a_score_file_error() -> None:
    huge = "x" * 200_000  # beyond the csv module's field size limit
    text = f"trace_id,label,score,cluster\na,benign,0.1,{huge}\n"
    with pytest.raises(ScoreFileError, match=r"bad\.csv:2: malformed CSV"):
        parse_csv(text, "bad.csv")


def test_csv_error_names_file_and_line() -> None:
    text = "# comment\ntrace_id,label,score,cluster\na,benign,0.1,r\nb,unsure,0.2,r\n"
    with pytest.raises(ScoreFileError, match=r"^bad\.csv:4: label"):
        parse_csv(text, "bad.csv")


def test_parse_jsonl_reads_every_column() -> None:
    text = (
        "# made-up rows\n"
        '{"trace_id": "a", "label": "benign", "score": 0.1, "cluster": "r1", '
        '"stage2_score": 0.2, "synthetic": true, "extra": 1}\n'
        "\n"
        '{"trace_id": 7, "label": "Violation", "score": "0.9", "cluster": 3, '
        '"synthetic": "false"}\n'
    )
    s = parse_jsonl(text, "x.jsonl")
    assert s.trace_ids == ("a", "7")
    assert s.labels == ("benign", "violation")
    assert s.scores.tolist() == [0.1, 0.9]
    assert s.clusters == ("r1", "3")
    assert s.stage2_scores is not None and math.isnan(s.stage2_scores[1])
    assert s.synthetic is not None and s.synthetic.tolist() == [True, False]
    assert s.ignored_columns == ("extra",)
    assert s.comments == ("made-up rows",)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("", "no data rows"),
        ("{bad\n", r":1: not valid JSON"),
        ("[1, 2]\n", r":1: each line must be a JSON object"),
        ('{"trace_id": "a"}\n', r":1: missing key\(s\) label, score, cluster"),
        (
            '{"trace_id": "a", "label": "benign", "score": true, "cluster": "r"}\n',
            r":1: score must be a number",
        ),
        (
            '{"trace_id": "a", "label": "benign", "score": NaN, "cluster": "r"}\n',
            r":1: score must be finite",
        ),
        (
            '{"trace_id": "a", "label": "benign", "score": null, "cluster": "r"}\n',
            r":1: score is empty",
        ),
        (
            '{"trace_id": "a", "label": "benign", "score": 1, "cluster": "r", "weight": 1}\n'
            '{"trace_id": "b", "label": "violation", "score": 1, "cluster": "r"}\n',
            r":2: missing key\(s\) weight",
        ),
    ],
)
def test_jsonl_errors(text: str, match: str) -> None:
    with pytest.raises(ScoreFileError, match=match):
        parse_jsonl(text, "bad.jsonl")


def test_read_scores_file_errors(tmp_path: Path) -> None:
    with pytest.raises(ScoreFileError, match="file not found"):
        read_scores(tmp_path / "missing.csv")
    folder = tmp_path / "folder.csv"
    folder.mkdir()
    with pytest.raises(ScoreFileError, match="is a directory"):
        read_scores(folder)
    binary = tmp_path / "binary.csv"
    binary.write_bytes(b"\xff\xfe\x00trace")
    with pytest.raises(ScoreFileError, match="not UTF-8"):
        read_scores(binary)
    unknown = tmp_path / "scores.txt"
    unknown.write_text("trace_id,label,score,cluster\na,benign,0,r\nb,violation,1,r\n")
    with pytest.raises(ScoreFileError, match="cannot tell the format"):
        read_scores(unknown)
    assert read_scores(unknown, "csv").n_traces == 2


def test_unreadable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "s.csv"
    path.write_text("x")

    def deny(self: Path) -> bytes:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_bytes", deny)
    with pytest.raises(ScoreFileError, match="cannot read: Permission denied"):
        read_scores(path)


def test_format_of() -> None:
    assert format_of("a.csv") == "csv"
    assert format_of("a.CSV") == "csv"
    assert format_of("a.jsonl") == "jsonl"
    assert format_of("a.ndjson") == "jsonl"
    assert format_of("anything", "jsonl") == "jsonl"
    with pytest.raises(ScoreFileError, match="unknown score file format"):
        format_of("a.csv", "xlsx")
    with pytest.raises(ScoreFileError, match=r"\(none\)"):
        format_of("noext")


def _set(**overrides: Any) -> ScoreSet:
    base: dict[str, Any] = {
        "trace_ids": ("a", "b"),
        "labels": ("benign", "violation"),
        "scores": np.array([0.1, 0.9]),
        "clusters": ("r", "s"),
    }
    base.update(overrides)
    return ScoreSet(**base)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"scores": np.array([0.1])}, "score has 1 values for 2 traces"),
        ({"scores": np.array([0.1, np.nan])}, "score values must be finite"),
        ({"scores": ["x", "y"]}, "score values must be numbers"),
        ({"labels": ("benign",)}, "one value per trace"),
        ({"labels": ("benign", "bad")}, "label must be"),
        ({"labels": ("benign", "benign")}, "no violation traces"),
        ({"labels": ("violation", "violation")}, "no benign traces"),
        ({"trace_ids": ("a", "a")}, "trace_id 'a' appears more than once"),
        ({"trace_ids": ("a", "")}, "non-empty trace_id"),
        ({"clusters": ("r", "")}, "non-empty cluster"),
        ({"weights": np.array([1.0, 0.0])}, "every weight must be positive"),
        ({"weights": np.array([1.0, np.inf])}, "weight values must be finite"),
        ({"synthetic": np.array([True])}, "synthetic has 1 values for 2 traces"),
        ({"stage2_scores": np.array([0.1, np.inf])}, "stage2_score values must be finite"),
        (
            {"trace_ids": (), "labels": (), "scores": np.array([]), "clusters": ()},
            "no scored traces",
        ),
    ],
)
def test_scoreset_validation(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(ScoreFileError, match=match):
        _set(**overrides)


def test_scoreset_accepts_nan_stage2_and_lists() -> None:
    s = _set(stage2_scores=[np.nan, 0.5], synthetic=[True, False], weights=[1, 2])
    assert s.stage2_scores is not None and math.isnan(s.stage2_scores[0])
    assert s.weights is not None and s.weights.dtype == np.float64
    rec = list(s.records())
    assert rec[0]["stage2_score"] is None and rec[1]["stage2_score"] == 0.5
    assert rec[1]["synthetic"] is False and rec[1]["weight"] == 2.0


def test_labels_constant() -> None:
    assert LABELS == ("benign", "violation")
