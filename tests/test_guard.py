"""Tests for the Claude billing guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from baserate import guard


def test_clean_environment_has_no_problems() -> None:
    assert guard.billing_guard_problems({"PATH": "/usr/bin", "HOME": "/h"}) == []


@pytest.mark.parametrize("name", guard.GUARDED_ENV_VARS)
def test_each_guarded_variable_is_reported(name: str) -> None:
    problems = guard.billing_guard_problems({name: "secret-value"})
    assert problems == [f"environment variable {name} is set"]
    assert "secret-value" not in " ".join(problems)


def test_provider_switch_prefix_is_reported() -> None:
    env = {"CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1", "OTHER": "x"}
    assert guard.billing_guard_problems(env) == [
        "environment variable CLAUDE_CODE_USE_BEDROCK is set",
        "environment variable CLAUDE_CODE_USE_VERTEX is set",
    ]


def test_api_key_helper_in_settings_is_reported(tmp_path: Path) -> None:
    with_helper = tmp_path / "a.json"
    with_helper.write_text(json.dumps({"apiKeyHelper": "/bin/echo"}))
    without = tmp_path / "b.json"
    without.write_text(json.dumps({"model": "x"}))
    broken = tmp_path / "c.json"
    broken.write_text("{not json")
    listy = tmp_path / "d.json"
    listy.write_text("[]")
    missing = tmp_path / "missing.json"
    problems = guard.billing_guard_problems({}, [with_helper, without, broken, listy, missing])
    assert problems == [f"apiKeyHelper is configured in {with_helper}"]


def test_settings_paths_cover_user_project_and_managed(tmp_path: Path) -> None:
    paths = guard.settings_paths(tmp_path / "home", tmp_path / "proj")
    assert tmp_path / "home" / ".claude" / "settings.json" in paths
    assert tmp_path / "proj" / ".claude" / "settings.local.json" in paths
    assert any("managed-settings" in p.name for p in paths)


def test_current_problems_reads_process_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in list(guard.GUARDED_ENV_VARS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("HOME", str(tmp_path))
    problems = guard.current_problems(tmp_path)
    assert "environment variable ANTHROPIC_API_KEY is set" in problems
