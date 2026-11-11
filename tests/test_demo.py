"""The offline demo is deterministic and matches its golden output."""

from __future__ import annotations

from pathlib import Path

import pytest

from baserate import demo

ROOT = Path(__file__).resolve().parents[1]


def test_demo_matches_golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASERATE_NO_MODEL_CALLS", "1")
    out = tmp_path / "demo.txt"
    assert demo.main(["--out", str(out)]) == 0
    assert out.read_text() == (ROOT / "demo" / "golden_output.txt").read_text()


def test_fixture_tampering_is_caught(tmp_path: Path) -> None:
    root = tmp_path / "armour"
    src = ROOT / "tests" / "fixtures" / "armour"
    for f in src.rglob("*"):
        if f.is_file():
            dest = root / f.relative_to(src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(f.read_bytes())
    target = root / "datasets" / "model_agent_runs.jsonl"
    target.write_text(target.read_text() + "\n")
    with pytest.raises(SystemExit, match="fixture check failed"):
        demo.verify_fixtures(root)
    (root / "README.md").write_text("no hashes here\n")
    with pytest.raises(SystemExit, match="no hashes"):
        demo.verify_fixtures(root)
