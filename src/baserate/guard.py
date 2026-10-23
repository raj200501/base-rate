"""Billing guard for Claude child processes.

Section 8 of the build brief forbids launching headless Claude children while
anything could route them to per-token billing: an API key, an auth token, a
custom base URL, any ``CLAUDE_CODE_USE_*`` provider switch, or an
``apiKeyHelper`` setting. This module detects those conditions. It reports
variable and file names only, never their values.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path

GUARDED_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)
GUARDED_ENV_PREFIX = "CLAUDE_CODE_USE_"
GUARDED_SETTING = "apiKeyHelper"


def settings_paths(home: Path, project: Path) -> list[Path]:
    """Return the Claude Code settings files that could carry ``apiKeyHelper``.

    Args:
        home: The user's home directory.
        project: The project root whose ``.claude/`` settings apply.

    Returns:
        Candidate paths in user, project, local, and managed scope. Paths
        that do not exist are included; callers skip them.
    """
    return [
        home / ".claude" / "settings.json",
        home / ".claude" / "settings.local.json",
        project / ".claude" / "settings.json",
        project / ".claude" / "settings.local.json",
        Path("/etc/claude-code/managed-settings.json"),
        Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    ]


def _setting_present(path: Path) -> bool:
    """Return True if ``path`` is a readable JSON object naming apiKeyHelper."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and GUARDED_SETTING in data


def billing_guard_problems(
    env: Mapping[str, str], settings_files: Iterable[Path] = ()
) -> list[str]:
    """List every condition that makes launching a Claude child unsafe.

    Args:
        env: The environment the child would inherit.
        settings_files: Settings files to inspect for ``apiKeyHelper``.

    Returns:
        Human-readable problems, naming variables or files but never values.
        An empty list means the guard allows Claude children.
    """
    problems = [f"environment variable {name} is set" for name in GUARDED_ENV_VARS if name in env]
    problems.extend(
        f"environment variable {name} is set"
        for name in sorted(env)
        if name.startswith(GUARDED_ENV_PREFIX)
    )
    problems.extend(
        f"{GUARDED_SETTING} is configured in {path}"
        for path in settings_files
        if path.is_file() and _setting_present(path)
    )
    return problems


def current_problems(project: Path | None = None) -> list[str]:
    """Apply :func:`billing_guard_problems` to this process's environment.

    Args:
        project: Project root; defaults to the current working directory.

    Returns:
        The guard problems for the current environment and settings files.
    """
    root = project if project is not None else Path.cwd()
    return billing_guard_problems(os.environ, settings_paths(Path.home(), root))
