# Ported from tools/chainlib.py of https://github.com/raj200501/raise-v1-public
# at commit 9330c9d396b328cef4736356e0c8962218f0ecd2, Copyright 2026 Raj Kashikar,
# licensed under the Apache License, Version 2.0 (third_party/raise-v1/LICENSE and
# third_party/raise-v1/NOTICE). Changed for base-rate: canonical JSON follows the
# contract in docs/PLAN.md (ASCII escapes, as plain json.dumps writes them), and the
# git helpers read history (commits touching a path, blobs, ancestry) for the
# preregistration and claim tools instead of recording HEAD and dirtiness.
"""Canonical JSON, hashing, and git helpers shared by the integrity tools.

Everything that must be tamper-evident goes through here, so there is exactly
one definition of "the bytes we hashed" and it is auditable in one place.
"Canonical JSON" is ``json.dumps(obj, sort_keys=True, separators=(",", ":"))``
(docs/PLAN.md, "Artifact contracts"), the same bytes tools/ship_check.py hashes.
Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


class GitError(Exception):
    """A git command failed; the message carries git's own error text."""


def canonical_json(obj: Any) -> str:
    """Canonical JSON text: sorted keys, no whitespace, ASCII escapes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sha256_obj(obj: Any) -> str:
    """Hex SHA-256 of the canonical JSON of ``obj`` (UTF-8 bytes)."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file's bytes, read in chunks."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_bytes(root: Path, *args: str) -> bytes:
    """Run git in ``root`` and return raw stdout; raise GitError on failure."""
    try:
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    except OSError as exc:
        raise GitError(f"cannot run git: {exc}") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"git {' '.join(args)} failed: {err}")
    return proc.stdout


def git(root: Path, *args: str) -> str:
    """Run git in ``root`` and return stripped stdout text; raise GitError on failure."""
    return git_bytes(root, *args).decode("utf-8", errors="replace").strip()


def repo_toplevel(root: Path) -> Path:
    """The top-level directory of the work tree containing ``root``.

    Raises:
        GitError: If ``root`` is not inside a git work tree or has no commits.
    """
    top = Path(git(root, "rev-parse", "--show-toplevel"))
    git(top, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    return top


def tracked_files(root: Path, *pathspecs: str) -> list[str]:
    """Paths in the index (tracked files), optionally limited by pathspecs."""
    out = git_bytes(root, "ls-files", "-z", "--", *pathspecs)
    return sorted(p for p in out.decode("utf-8", errors="replace").split("\0") if p)


def untracked_files(root: Path, *pathspecs: str) -> list[str]:
    """Untracked, non-ignored paths, optionally limited by pathspecs."""
    out = git_bytes(root, "ls-files", "-z", "--others", "--exclude-standard", "--", *pathspecs)
    return sorted(p for p in out.decode("utf-8", errors="replace").split("\0") if p)


def history_paths(root: Path, prefix: str) -> list[str]:
    """Every path under ``prefix`` that any commit in HEAD's history touched.

    Renames are reported as a deletion plus an addition, so both names appear.
    """
    out = git(
        root, "log", "--full-history", "--no-renames", "--format=", "--name-only", "--", prefix
    )
    return sorted({ln for ln in out.splitlines() if ln.strip()})


def commits_touching(
    root: Path, path: str, *, merges: bool = False, oldest_first: bool = False
) -> list[str]:
    """Commits in HEAD's history that touched ``path``, newest first.

    Uses ``--full-history`` (side branches count) and ``--no-renames`` (a
    rename is a deletion of the old name and an addition of the new one).
    Merge commits are left out unless ``merges`` is true. With
    ``oldest_first`` the list is in reverse topological order, so a commit
    always comes after its ancestors even when commit dates tie.
    """
    args = ["log", "--full-history", "--no-renames", "--format=%H"]
    if not merges:
        args.append("--no-merges")
    if oldest_first:
        args += ["--topo-order", "--reverse"]
    return git(root, *args, "--", path).splitlines()


def commits_following(root: Path, path: str) -> list[str]:
    """Commits that touched ``path`` or, through renames, its earlier names."""
    return git(root, "log", "--follow", "--format=%H", "--", path).splitlines()


def change_status(root: Path, commit: str, path: str) -> str:
    """Status letter (A, M, D, T) of ``path`` in a non-merge ``commit``, or ''."""
    out = git(root, "show", "--no-renames", "--format=", "--name-status", commit, "--", path)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return lines[0].split("\t", 1)[0] if lines else ""


def blob_id(root: Path, rev: str, path: str) -> str | None:
    """Object id of ``path`` at ``rev``, or None if it does not exist there."""
    try:
        return git(root, "rev-parse", "--verify", "--quiet", f"{rev}:{path}")
    except GitError:
        return None


def blob_bytes(root: Path, oid: str) -> bytes:
    """Contents of a blob object."""
    return git_bytes(root, "cat-file", "blob", oid)


def hash_object(root: Path, path: Path) -> str:
    """Object id git would give the file at ``path`` (no write)."""
    return git(root, "hash-object", "--", str(path))


def is_strict_ancestor(root: Path, older: str, newer: str) -> bool:
    """True if ``older`` is an ancestor of ``newer`` and not the same commit."""
    if older == newer:
        return False
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", older, newer],
        cwd=root,
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0
