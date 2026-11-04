"""Helpers for the integrity-tool tests: throwaway git repositories and tool loading.

Every repository here lives under pytest's tmp_path. Git runs with no global
or system configuration and a fixed identity, so results do not depend on the
machine (signing, rename settings, default branch names).
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOLS = REPO_ROOT / "tools"
FIXTURES = Path(__file__).resolve().parent
CLAIMS_REPO = FIXTURES / "claims_repo"

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Integrity Test",
    "GIT_AUTHOR_EMAIL": "integrity-test@example.invalid",
    "GIT_COMMITTER_NAME": "Integrity Test",
    "GIT_COMMITTER_EMAIL": "integrity-test@example.invalid",
}


def isolate_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every git process in this test ignore machine-level configuration."""
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


def load_tool(name: str) -> ModuleType:
    """Import ``tools/<name>.py`` (tools/ goes on sys.path so siblings resolve)."""
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    module = importlib.import_module(name)
    assert Path(module.__file__ or "").resolve() == TOOLS / f"{name}.py", module.__file__
    return module


class Repo:
    """A small git repository for one test."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def init(cls, path: Path) -> Repo:
        """Create an empty repository on branch main."""
        path.mkdir(parents=True, exist_ok=True)
        repo = cls(path)
        repo.git("init", "-q", "-b", "main")
        return repo

    def git(self, *args: str) -> str:
        """Run git here and return stripped stdout."""
        proc = subprocess.run(
            ["git", *args],
            cwd=self.path,
            env={**os.environ, **GIT_ENV},
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
        return proc.stdout.strip()

    def write(self, rel: str, text: str) -> Path:
        """Write a text file (parents created)."""
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def write_json(self, rel: str, obj: Any) -> Path:
        """Write a JSON file."""
        return self.write(rel, json.dumps(obj, indent=2) + "\n")

    def read(self, rel: str) -> str:
        """Read a text file."""
        return (self.path / rel).read_text(encoding="utf-8")

    def read_json(self, rel: str) -> Any:
        """Read a JSON file."""
        return json.loads(self.read(rel))

    def copy_in(self, src: Path, rel: str) -> None:
        """Copy a file or directory tree into the repository."""
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, target, dirs_exist_ok=True)
        else:
            shutil.copy2(src, target)

    def commit(self, message: str = "commit", *paths: str) -> str:
        """Stage ``paths`` (default: everything) and commit; return the new HEAD."""
        if paths:
            self.git("add", "-f", "--", *paths)
        else:
            self.git("add", "-A")
        self.git("commit", "-q", "--no-verify", "-m", message)
        return self.head()

    def head(self) -> str:
        """Full SHA of HEAD."""
        return self.git("rev-parse", "HEAD")

    def copy_to(self, dest: Path) -> Repo:
        """A copy of this repository (history included) at ``dest``."""
        shutil.copytree(self.path, dest, symlinks=True)
        return Repo(dest)


def prereg_text(
    config: dict[str, Any],
    *,
    pid: str = "0001",
    title: str = "Test plan",
    experiment: str = "E2",
    date: str = "2026-10-23",
    extra: str = "",
    body: str = "Plan prose.\n",
) -> str:
    """A preregistration that meets the contract, with knobs to break it."""
    front = f"---\nid: {pid}\ntitle: {title}\nexperiment: {experiment}\ndate: {date}\n{extra}---\n"
    block = "```json prereg-config\n" + json.dumps(config, sort_keys=True) + "\n```\n"
    return f"{front}\n# {title}\n\n{body}\n{block}"
