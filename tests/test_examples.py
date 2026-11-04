"""The three documented examples run offline and fast, and their fixtures are up to date."""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from baserate.scores import read_scores

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
LIMIT_S = 30.0
DEAD_PROXY = "http://127.0.0.1:9"


@functools.cache
def _offline_prefix() -> tuple[str, ...]:
    """``unshare -rn`` (no network) when this machine allows it, as gate G9 does."""
    if sys.platform.startswith("linux") and shutil.which("unshare"):
        probe = subprocess.run(["unshare", "-rn", "true"], capture_output=True, check=False)
        if probe.returncode == 0:
            return ("unshare", "-rn")
    return ()


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "BASERATE_NO_MODEL_CALLS": "1",
        "HTTP_PROXY": DEAD_PROXY,
        "HTTPS_PROXY": DEAD_PROXY,
        "http_proxy": DEAD_PROXY,
        "https_proxy": DEAD_PROXY,
        "NO_PROXY": "",
    }
    return subprocess.run(
        [*_offline_prefix(), sys.executable, str(script), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "evaluate_your_monitor.py",
            [
                "budget 20/10k at prevalence 0.001: recall ",
                "# Monitor report card: rule:armour-reviewer-calibrated",
                "## Certification gap",
                "more independent benign runs with no false positive are needed",
            ],
        ),
        (
            "certify.py",
            [
                "Plan (design targets only, one-sided 95%):",
                "independent benign runs, none with a false positive",
                "more independent benign runs with no false positive are needed",
            ],
        ),
        (
            "build_cascade.py",
            ["Pareto front at prevalence 0.001, budget 20/10k:", "Best under cost 3:"],
        ),
    ],
)
def test_example_runs_offline_and_fast(name: str, expected: list[str]) -> None:
    start = time.monotonic()
    proc = _run(EXAMPLES / name)
    elapsed = time.monotonic() - start
    assert proc.returncode == 0, proc.stderr
    assert elapsed < LIMIT_S
    for text in expected:
        assert text in proc.stdout
    assert "not study results" in proc.stdout


def test_examples_say_their_data_are_demonstration_data() -> None:
    for name in ("evaluate_your_monitor.py", "certify.py", "build_cascade.py"):
        text = (EXAMPLES / name).read_text(encoding="utf-8")
        assert "demonstration data, not study results" in text.lower()


def test_committed_fixtures_are_up_to_date() -> None:
    proc = _run(EXAMPLES / "fixtures" / "make_fixtures.py", "--check")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.count("up to date") == 2


def test_fixture_generator_is_deterministic(tmp_path: Path) -> None:
    proc = _run(EXAMPLES / "fixtures" / "make_fixtures.py", "--out", str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    for name in ("monitor_scores.csv", "cascade_scores.csv"):
        assert (tmp_path / name).read_bytes() == (EXAMPLES / "fixtures" / name).read_bytes()
    (tmp_path / "monitor_scores.csv").write_text("stale\n")
    stale = _run(EXAMPLES / "fixtures" / "make_fixtures.py", "--check", "--out", str(tmp_path))
    assert stale.returncode == 1
    assert "monitor_scores.csv: out of date" in stale.stdout


def test_fixtures_label_their_source() -> None:
    for name in ("monitor_scores.csv", "cascade_scores.csv"):
        scores = read_scores(EXAMPLES / "fixtures" / name)
        joined = " ".join(scores.comments)
        assert "not study results" in joined
        assert "MIT License" in joined and "639eb62" in joined
        assert scores.synthetic is not None
    assert read_scores(EXAMPLES / "fixtures" / "cascade_scores.csv").has_stage2


def test_examples_are_documented() -> None:
    readme = (EXAMPLES / "README.md").read_text(encoding="utf-8")
    for name in ("evaluate_your_monitor.py", "certify.py", "build_cascade.py"):
        assert f"examples/{name}" in readme
    fixtures = (EXAMPLES / "fixtures" / "README.md").read_text(encoding="utf-8")
    assert "MIT" in fixtures and "639eb62" in fixtures
    assert "not study results" in fixtures
