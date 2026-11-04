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


REGULAR_MODES = frozenset({"100644", "100755"})
"""Git modes of regular files (a symlink, 120000, is not one)."""
_ZERO = "0" * 40


def history(root: Path) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    """Every commit in HEAD's history, parents before children, and each one's parents."""
    order: list[str] = []
    parents: dict[str, tuple[str, ...]] = {}
    for line in git(
        root, "rev-list", "--topo-order", "--reverse", "--parents", "HEAD"
    ).splitlines():
        commit, *ps = line.split()
        order.append(commit)
        parents[commit] = tuple(ps)
    return order, parents


def path_states(
    root: Path, path: str, order: list[str], parents: dict[str, tuple[str, ...]]
) -> dict[str, tuple[str, str] | None]:
    """The (blob id, mode) of ``path`` at every commit of ``order``, or None where absent.

    Merge commits count like any other: a merge's state is its own tree's,
    including a merge whose content differs from every parent. One ``git log``
    lists every commit whose state differs from a parent's; every other commit
    keeps its first parent's state.
    """
    changed: dict[str, tuple[str, str] | None] = {}
    current = ""
    out = git(
        root,
        "log",
        "--full-history",
        "-m",
        "--no-renames",
        "--raw",
        "--no-abbrev",
        "--format=%x00%H",
        "--",
        path,
    )
    for line in out.splitlines():
        if line.startswith("\0"):
            current = line[1:]
        elif line.startswith(":") and current:
            fields = line.split("\t", 1)[0].split()
            mode, blob = fields[1], fields[3]
            changed[current] = None if blob == _ZERO else (blob, mode)
    states: dict[str, tuple[str, str] | None] = {}
    for commit in order:
        if commit in changed:
            states[commit] = changed[commit]
        elif parents[commit]:
            states[commit] = states.get(parents[commit][0])
        else:
            states[commit] = None
    return states


def introducing(
    order: list[str],
    parents: dict[str, tuple[str, ...]],
    states: dict[str, tuple[str, str] | None],
) -> list[str]:
    """Commits whose state of a path exists and differs from every parent's, oldest first."""
    return [
        c
        for c in order
        if states[c] is not None and all(states.get(p) != states[c] for p in parents[c])
    ]


def descendants(root: Path, commit: str) -> frozenset[str]:
    """Commits in HEAD's history that have ``commit`` as a strict ancestor."""
    out = git(root, "rev-list", "--ancestry-path", f"{commit}..HEAD")
    return frozenset(out.split())


def rename_sources(root: Path, path: str) -> list[str]:
    """``path`` and every earlier name it was renamed from (true renames only, not copies)."""
    out = git(
        root,
        "log",
        "--full-history",
        "-m",
        "-M",
        "--diff-filter=R",
        "--name-status",
        "--format=",
        "HEAD",
    )
    renamed: dict[str, set[str]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].startswith("R"):
            renamed.setdefault(parts[2], set()).add(parts[1])
    names, todo = [path], [path]
    while todo:
        for old in sorted(renamed.get(todo.pop(), ())):
            if old not in names:
                names.append(old)
                todo.append(old)
    return names


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
