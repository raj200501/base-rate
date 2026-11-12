"""Tests for the repository tools (secrets scan, ship check helpers, review validation)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


secrets_scan = _load("secrets_scan")
ship_check = _load("ship_check")
review = _load("review")


@pytest.mark.parametrize(
    "fake",
    [
        "sk-ant-" + "a" * 30,
        "AKIA" + "A" * 16,
        "ghp_" + "b" * 36,
        "AIza" + "c" * 35,
        "-----BEGIN RSA " + "PRIVATE KEY-----",
        "hf_" + "d" * 34,
    ],
)
def test_secret_patterns_match_fakes(fake: str) -> None:
    findings = secrets_scan.scan_text(f"x = '{fake}'\n", "f.py")
    assert findings and findings[0].startswith("f.py:1:")
    assert fake not in " ".join(findings)


def test_secret_patterns_ignore_ordinary_text() -> None:
    text = "sk-learn is a library\nAKIA is a prefix\nhf_hub_download(...)\n"
    assert secrets_scan.scan_text(text, "f.py") == []


def test_env_leak_detection_names_variable_not_value() -> None:
    values = secrets_scan.env_values(
        {
            "MY_API_KEY": "v" * 20,
            "SHORT_TOKEN": "abc",
            "PATH_KEY": "/usr/local/bin/x",
            "HOME": "x" * 30,
        }
    )
    assert list(values) == ["MY_API_KEY"]
    findings = secrets_scan.scan_env_leaks("prefix " + "v" * 20, "f.txt", values)
    assert findings == ["f.txt: value of $MY_API_KEY appears"]


def test_read_for_scan_handles_gzip_and_binary(tmp_path: Path) -> None:
    import gzip

    gz = tmp_path / "s.jsonl.gz"
    gz.write_bytes(gzip.compress(b'{"a": 1}\n'))
    assert secrets_scan.read_for_scan(gz) == '{"a": 1}\n'
    binary = tmp_path / "b.bin"
    binary.write_bytes(b"\0\1\2")
    assert secrets_scan.read_for_scan(binary) is None


def test_canonical_hash_is_key_order_independent() -> None:
    assert ship_check.canonical_sha256({"a": 1, "b": [1, 2]}) == ship_check.canonical_sha256(
        {"b": [1, 2], "a": 1}
    )


def test_md_sections_keep_a_code_block_that_opens_a_section() -> None:
    """The heading match must not run on through a blanked code block (G13's diagram check)."""
    text = (
        "# T\n\n## How it works\n\n```mermaid\nflowchart LR\n  A --> B\n```\n\n"
        "Prose.\n\n## Next\nx\n"
    )
    body = dict(ship_check.md_sections(text))["How it works"]
    assert "```mermaid" in body and "Prose." in body


def test_md_sections_ignore_code_blocks() -> None:
    text = "# T\n\n## One\nbody\n```\n## not a heading\n```\n## Two\nmore\n"
    heads = [h for h, _ in ship_check.md_sections(text)]
    assert heads == ["One", "Two"]


def test_word_count_skips_code() -> None:
    assert ship_check.word_count("one two\n```\nthree four five\n```\nsix") == 3


def test_status_line_ignores_stderr_after_it() -> None:
    """A tool's status line counts even when a warning on stderr follows it (G10, G12, G13)."""
    out = "REPRODUCE: PASS 4 checks\nwarning: UV_NATIVE_TLS is deprecated\n"
    proc = ship_check.Proc(0, out, 1.0)
    assert proc.tail() == "warning: UV_NATIVE_TLS is deprecated"
    assert proc.status("REPRODUCE:") == "REPRODUCE: PASS 4 checks"
    assert ship_check.Proc(0, "noise\nREVIEW: BLOCK\nREVIEW: SHIP\n", 1.0).status("REVIEW:") == (
        "REVIEW: SHIP"
    )
    assert ship_check.Proc(1, "nothing here\n", 1.0).status("REPRODUCE:") == ""


def test_trunc_limits_evidence_to_one_short_line() -> None:
    out = ship_check.trunc("a\nb " + "x" * 500)
    assert "\n" not in out and len(out) <= ship_check.EVIDENCE_MAX


def test_need_raises_gate_failure() -> None:
    with pytest.raises(ship_check.GateError, match="why"):
        ship_check.need(False, "why")
    ship_check.need(True, "unused")


def _review(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema": "baserate-review/1",
        "reviewed_commit": "a" * 40,
        "reviewed_at": "2026-11-12T01:53:00Z",
        "reviewer": "subagent",
        "verdict": "SHIP",
        "blocking": [],
        "non_blocking": [{"id": "N1", "title": "t"}],
        "final_line": "REVIEW: SHIP",
    }
    base.update(overrides)
    return base


def test_review_validation_accepts_well_formed_ship() -> None:
    assert review.validate(_review()) == []


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        (
            {
                "verdict": "SHIP",
                "blocking": [{"id": "B1", "title": "t", "location": "f", "evidence": "e"}],
            },
            "SHIP with open blocking",
        ),
        ({"verdict": "BLOCK", "final_line": "REVIEW: BLOCK"}, "BLOCK without"),
        ({"final_line": "REVIEW: BLOCK"}, "final_line"),
        ({"reviewed_commit": "abc"}, "40-character"),
        (
            {"blocking": [{"id": "B1"}], "verdict": "BLOCK", "final_line": "REVIEW: BLOCK"},
            "needs id, title",
        ),
        ({"schema": "other"}, "schema"),
        ({"reviewer": "me"}, "reviewer"),
        ({"blocking": None}, "must be lists"),
        ({"reviewed_at": None}, "reviewed_at"),
        ({"reviewed_at": "yesterday"}, "reviewed_at"),
        ({"reviewed_at": "2026-11-12T01:53:00"}, "reviewed_at"),
    ],
)
def test_review_validation_rejects_inconsistent_reviews(
    overrides: dict[str, object], needle: str
) -> None:
    problems = review.validate(_review(**overrides))
    assert any(needle in p for p in problems), problems


def test_review_validation_rejects_non_object() -> None:
    assert review.validate([]) == ["review is not a JSON object"]


def test_g13_prose_helpers_catch_unwritten_sections() -> None:
    assert ship_check.leftover_markers("a <!-- @prose finding --> b <!-- @table x -->") == [
        "<!-- @prose finding -->",
        "<!-- @table x -->",
    ]
    assert ship_check.leftover_markers("<!-- a plain comment -->") == []
    table_only = "# base-rate\n\n*Recall [95% interval]*\n\n| m | 0.41 [0.30, 0.52] |\n"
    assert not ship_check.has_finding_sentence(table_only)
    prose = table_only + "\nThe best monitor caught 0.41 [0.30, 0.52] of violations.\n"
    assert ship_check.has_finding_sentence(prose)
    in_code = "```\nx = [0.1, 0.2]\n```\n"
    assert not ship_check.has_finding_sentence(in_code)
    assert not ship_check.section_has_text("\n<!-- @prose readme-limitations -->\n")
    assert ship_check.section_has_text("Results describe these monitors only.\n")
