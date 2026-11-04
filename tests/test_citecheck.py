"""Tests for tools/citecheck.py using temporary repositories."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "tools" / "citecheck.py"
    spec = importlib.util.spec_from_file_location("citecheck_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["citecheck_under_test"] = module
    spec.loader.exec_module(module)
    return module


cc = _load()


def entry(key: str, **over: Any) -> dict[str, Any]:
    e: dict[str, Any] = {
        "key": key,
        "claimed": {"title": "T", "authors": "A", "year": 2025, "url": "https://example.org/x"},
        "retrieved_at": "2026-11-04T01:00:00Z",
        "final_url": "https://example.org/x",
        "page_title": "T page",
        "content_sha256": "a" * 64,
        "verified": True,
        "notes": "matches",
    }
    e.update(over)
    return e


def make_repo(
    root: Path,
    entries: list[Any],
    readme: str | None = "See [@one].\n",
    report: str | None = "Also [@two] and [@one].\n",
) -> Path:
    (root / "artifacts").mkdir()
    (root / "docs").mkdir()
    (root / "artifacts" / "citecheck.json").write_text(
        json.dumps({"schema": "baserate-citecheck/1", "references": entries})
    )
    if readme is not None:
        (root / "README.md").write_text(readme)
    if report is not None:
        (root / "docs" / "REPORT.md").write_text(report)
    return root


def test_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_repo(tmp_path, [entry("one"), entry("two"), entry("unused", verified=False)])
    assert cc.main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == "CITECHECK: PASS 2/2 verified"


def test_missing_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_repo(tmp_path, [entry("one")])
    assert cc.main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert any(line.startswith("two: cited in docs/REPORT.md but not in") for line in out)
    assert out[-1] == "CITECHECK: FAIL 1/2 verified"


def test_unverified_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_repo(tmp_path, [entry("one"), entry("two", verified=False, page_title=None)])
    assert cc.main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert any("two: cited in docs/REPORT.md but not verified" in line for line in out)
    assert out[-1] == "CITECHECK: FAIL 1/2 verified"


@pytest.mark.parametrize(
    "over",
    [
        {"content_sha256": "xyz"},
        {"retrieved_at": "yesterday"},
        {"verified": "yes"},
        {"notes": ""},
        {"final_url": None},
        {"claimed": {"title": "", "url": "ftp://x"}},
    ],
)
def test_malformed_entry(tmp_path: Path, over: dict[str, Any]) -> None:
    make_repo(tmp_path, [entry("one"), entry("two", **over)])
    problems, verified, total = cc.run(tmp_path)
    assert (verified, total) == (1, 2)
    assert any(p.startswith("entry two:") for p in problems)


def test_verified_needs_page_title(tmp_path: Path) -> None:
    make_repo(tmp_path, [entry("one"), entry("two", page_title=None)])
    problems, verified, _ = cc.run(tmp_path)
    assert verified == 1
    assert any("page_title is empty" in p for p in problems)


def test_null_hash_allowed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_repo(tmp_path, [entry("one", content_sha256=None), entry("two")])
    assert cc.main(["--root", str(tmp_path)]) == 0
    assert "PASS 2/2" in capsys.readouterr().out


def test_missing_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_repo(tmp_path, [entry("one"), entry("two")], report=None)
    assert cc.main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert "docs/REPORT.md: document missing" in out
    assert out[-1].startswith("CITECHECK: FAIL")


def test_no_citations_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_repo(tmp_path, [entry("one")], readme="none\n", report="none\n")
    assert cc.main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "CITECHECK: FAIL 0/0 verified"
    assert any("at least one" in line for line in out)


def test_code_is_ignored_and_templates_scanned(tmp_path: Path) -> None:
    make_repo(
        tmp_path,
        [entry("one"), entry("two")],
        readme="Use `[@fake]` like this.\n\n```\n[@fake2]\n```\n\nReal [@one].\n",
    )
    (tmp_path / "docs" / "templates").mkdir()
    (tmp_path / "docs" / "templates" / "r.md").write_text("[@two] and [@absent]\n")
    problems, verified, total = cc.run(tmp_path)
    assert (verified, total) == (2, 3)
    assert any(p.startswith("absent:") for p in problems)
    assert not any("fake" in p for p in problems)


def test_bad_json_and_duplicate(tmp_path: Path) -> None:
    make_repo(tmp_path, [entry("one"), entry("one"), entry("two")])
    problems, verified, total = cc.run(tmp_path)
    assert any("duplicate key" in p for p in problems)
    assert (verified, total) == (1, 2)
    (tmp_path / "artifacts" / "citecheck.json").write_text("{not json")
    problems, verified, _ = cc.run(tmp_path)
    assert verified == 0 and any("cannot read or parse" in p for p in problems)


def test_missing_file_and_bad_schema(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("[@one]\n")
    (tmp_path / "docs" / "REPORT.md").write_text("x\n")
    problems, verified, total = cc.run(tmp_path)
    assert (verified, total) == (0, 1)
    assert any("file not found" in p for p in problems)
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "citecheck.json").write_text('{"schema": "other"}')
    problems, _, _ = cc.run(tmp_path)
    assert any("schema must be" in p for p in problems)


def test_real_citecheck_file_is_well_formed() -> None:
    root = Path(__file__).resolve().parents[1]
    status, problems = cc.load_entries(root / "artifacts" / "citecheck.json")
    assert problems == []
    assert status["clopper-pearson-1934"] is True
    assert status["openai-monitoring-2026"] is False
