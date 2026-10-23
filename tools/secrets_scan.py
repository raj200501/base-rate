"""Scan the repository for secrets (``make secrets-scan``).

Checks every tracked file (gzip shards are decompressed), every line added in
git history, and whether the value of any secret-looking environment variable
appears in a tracked file. Findings name the file, line, and pattern, never
the matched text. Stdlib only. Exit 0 when clean, 1 otherwise.
"""

from __future__ import annotations

import argparse
import gzip
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic-key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "openai-key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    "openrouter-key": re.compile(r"sk-or-v1-[0-9a-f]{40,}"),
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github-token": re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{36}\b"),
    "github-pat": re.compile(r"github_pat_[A-Za-z0-9_]{50,}"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "huggingface-token": re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),
    "slack-token": re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    "private-key-block": re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
}
SECRET_ENV_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)
MIN_ENV_VALUE_LEN = 16


def scan_text(text: str, where: str) -> list[str]:
    """Return findings for one text blob, one per matching line and pattern.

    Args:
        text: Content to scan.
        where: Label used in findings, such as a path.

    Returns:
        Findings of the form ``where:line: pattern``.
    """
    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        findings.extend(
            f"{where}:{lineno}: {name}" for name, pat in PATTERNS.items() if pat.search(line)
        )
    return findings


def env_values(env: dict[str, str]) -> dict[str, str]:
    """Secret-looking environment variables whose values are long enough to match."""
    return {
        k: v
        for k, v in env.items()
        if SECRET_ENV_NAME.search(k) and len(v) >= MIN_ENV_VALUE_LEN and not v.startswith("/")
    }


def scan_env_leaks(text: str, where: str, values: dict[str, str]) -> list[str]:
    """Findings for environment secret values that appear verbatim in ``text``."""
    return [f"{where}: value of ${name} appears" for name, v in values.items() if v in text]


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout


def tracked_files(root: Path) -> list[Path]:
    """Tracked paths (index), relative to ``root``."""
    out = _git(root, "ls-files", "-z")
    return [Path(p) for p in out.decode().split("\0") if p]


def read_for_scan(path: Path) -> str | None:
    """Decode a file for scanning; gzip is decompressed; other binaries are skipped."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if path.suffix == ".gz":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError):
            return None
    if b"\0" in raw[:8192]:
        return None
    return raw.decode("utf-8", errors="replace")


def history_added_lines(root: Path) -> Iterator[tuple[str, str]]:
    """Yield (commit, line) for every line added anywhere in git history."""
    try:
        out = _git(
            root, "log", "--all", "-p", "--no-color", "--no-ext-diff", "-U0", "--format=commit %H"
        )
    except subprocess.CalledProcessError:
        return
    commit = "?"
    for raw in out.decode("utf-8", errors="replace").splitlines():
        if raw.startswith("commit "):
            commit = raw.split()[1][:12]
        elif raw.startswith("+") and not raw.startswith("+++"):
            yield commit, raw[1:]


def scan_repo(root: Path, env: dict[str, str], history: bool = True) -> tuple[list[str], int]:
    """Scan tracked files and, optionally, history.

    Args:
        root: Repository root.
        env: Environment whose secret values must not appear in files.
        history: Also scan lines added in every commit.

    Returns:
        (findings, number of files scanned).
    """
    values = env_values(env)
    findings: list[str] = []
    scanned = 0
    for rel in tracked_files(root):
        text = read_for_scan(root / rel)
        if text is None:
            continue
        scanned += 1
        findings.extend(scan_text(text, str(rel)))
        findings.extend(scan_env_leaks(text, str(rel), values))
    if history:
        findings.extend(_scan_history(history_added_lines(root)))
    return findings, scanned


def _scan_history(lines: Iterable[tuple[str, str]]) -> list[str]:
    found: list[str] = []
    for commit, line in lines:
        found.extend(
            f"history {commit}: {name}" for name, pat in PATTERNS.items() if pat.search(line)
        )
    return sorted(set(found))


def main(argv: list[str] | None = None) -> int:
    """Run the scan and print findings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--no-history", action="store_true")
    args = parser.parse_args(argv)
    findings, scanned = scan_repo(args.root, dict(os.environ), history=not args.no_history)
    for f in findings:
        print(f)
    if findings:
        print(f"secrets-scan: FAIL ({len(findings)} findings in {scanned} files)")
        return 1
    history = "skipped" if args.no_history else "scanned"
    print(f"secrets-scan: clean ({scanned} files, history {history})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
