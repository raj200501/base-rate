#!/usr/bin/env python3
# Ported from tools/prereg.py of https://github.com/raj200501/raise-v1-public
# at commit 9330c9d396b328cef4736356e0c8962218f0ecd2, Copyright 2026 Raj Kashikar,
# licensed under the Apache License, Version 2.0 (third_party/raise-v1/LICENSE and
# third_party/raise-v1/NOTICE). Changed for base-rate: preregistrations are Markdown
# files with front matter and one ```json prereg-config``` block (docs/PLAN.md),
# frozen by their git commit instead of an append-only chain file with beacon
# anchors; ``verify`` reads git history and checks the artifacts that cite each
# preregistration; ``new``, ``freeze``, and ``show`` are replaced by ``hash``.
"""Preregistration integrity (``make prereg-verify``) and config hashing.

The contract (docs/PLAN.md "Artifact contracts", prereg/README.md):

- Every ``prereg/NNNN-name.md`` (``prereg/README.md`` excepted) starts with
  front matter between ``---`` lines holding ``id``, ``title``,
  ``experiment``, and ``date`` (ISO ``YYYY-MM-DD``), and has exactly one
  fenced block tagged ``json prereg-config`` holding a JSON object.
- A preregistration is frozen by its commit: exactly one ordinary commit
  touches it (the one that adds it), and it is never modified, renamed, or
  deleted afterwards. Its content at HEAD and in the working tree equals what
  that commit added.
- An amendment names the original in front matter (``amends: prereg/....md``);
  the original must exist and be committed strictly earlier.
- Every tracked JSON file under ``artifacts/`` with a ``prereg`` field cites
  an existing preregistration, carries ``prereg_config_sha256`` equal to the
  SHA-256 of the canonical JSON of that preregistration's config block, and
  every commit that touched the artifact comes strictly after the commit that
  froze the preregistration. An artifact with ``"confirmatory": true`` must
  cite one. Exploratory artifacts omit the ``prereg`` field.

Commands:
  verify [--root DIR]       check all of the above using DIR's git history
  hash PATH [--root DIR]    validate PATH and print its config hash

The last stdout line of ``verify`` is ``PREREG-VERIFY: PASS <n> preregistrations,
<m> linked artifacts`` or ``PREREG-VERIFY: FAIL <reason count>`` with the reasons
above it. Exit 0 only on PASS. Stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from chainlib import (  # noqa: E402
    GitError,
    blob_id,
    change_status,
    commits_following,
    commits_touching,
    hash_object,
    history_paths,
    is_strict_ancestor,
    repo_toplevel,
    sha256_obj,
    tracked_files,
    untracked_files,
)

PREREG_DIR = "prereg/"
META_FILES = frozenset({"prereg/README.md", "prereg/.gitkeep"})
NAME_RE = re.compile(r"^prereg/(\d{4})-[A-Za-z0-9][A-Za-z0-9._-]*\.md$")
# The same block pattern tools/ship_check.py uses, so both hash the same bytes.
CONFIG_RE = re.compile(r"```json prereg-config\n(.*?)```", re.S)
FRONT_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:[ \t]*(.*?)[ \t]*$")
REQUIRED_KEYS = ("id", "title", "experiment", "date")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PreregError(ValueError):
    """A preregistration file does not meet the contract."""


@dataclass
class Prereg:
    """One preregistration file as read from the working tree."""

    path: str
    front: dict[str, str] = field(default_factory=dict)
    config_sha256: str | None = None
    add_commit: str | None = None


def parse_front_matter(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse ``key: value`` lines between the opening and closing ``---`` lines.

    Indented lines, list items, and comments are ignored; only the unindented
    ``key: value`` lines count. Surrounding quotes on a value are removed.

    Returns:
        The parsed keys and a list of problems (empty when well formed).
    """
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return {}, ["no front matter (the file must start with a '---' line)"]
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].rstrip("\r") == "---")
    except StopIteration:
        return {}, ["front matter is not closed by a '---' line"]
    front: dict[str, str] = {}
    problems: list[str] = []
    for line in lines[1:end]:
        m = FRONT_KEY_RE.match(line.rstrip("\r"))
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in front:
            problems.append(f"front matter key {key!r} appears twice")
        front[key] = value
    return front, problems


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def parse_config(text: str) -> dict[str, Any]:
    """Parse the single ``json prereg-config`` block of a preregistration.

    Raises:
        PreregError: If there is not exactly one block, or it is not a JSON
            object, or it has duplicate keys or NaN/Infinity values.
    """
    blocks = CONFIG_RE.findall(text)
    if len(blocks) != 1:
        raise PreregError(f"needs exactly one ```json prereg-config``` block, found {len(blocks)}")
    try:
        config = json.loads(
            blocks[0], object_pairs_hook=_no_duplicates, parse_constant=_reject_constant
        )
    except ValueError as exc:
        raise PreregError(f"prereg-config block is not valid JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise PreregError("prereg-config block must hold a JSON object")
    return config


def config_sha256(text: str) -> str:
    """SHA-256 of the canonical JSON of a preregistration's config block."""
    return sha256_obj(parse_config(text))


def check_document(text: str) -> tuple[dict[str, str], str | None, list[str]]:
    """Check one preregistration's content against the contract.

    Returns:
        (front matter, config hash or None if the block is invalid, problems).
    """
    front, problems = parse_front_matter(text)
    if front or not problems:
        for key in REQUIRED_KEYS:
            if not front.get(key, "").strip():
                problems.append(f"front matter lacks {key!r}")
        date = front.get("date", "")
        if date and not _is_iso_date(date):
            problems.append(f"front matter date {date!r} is not an ISO date (YYYY-MM-DD)")
    try:
        digest: str | None = config_sha256(text)
    except PreregError as exc:
        digest = None
        problems.append(str(exc))
    return front, digest, problems


def _is_iso_date(value: str) -> bool:
    if not DATE_RE.match(value):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _short(commit: str | None) -> str:
    return commit[:12] if commit else "none"


def _read(root: Path, rel: str) -> str | None:
    try:
        return (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _check_frozen(root: Path, path: str, problems: list[str]) -> str | None:
    """Check that one tracked preregistration is untouched since its commit.

    Returns:
        The commit that added it, or None if that cannot be determined.
    """
    commits = commits_touching(root, path)
    if not commits:
        problems.append(f"{path}: tracked but not added by any ordinary commit")
        return None
    add = commits[-1]
    if len(commits) > 1:
        shas = ", ".join(_short(c) for c in reversed(commits))
        problems.append(
            f"{path}: changed after it was frozen; touched by {len(commits)} commits ({shas})"
        )
    if change_status(root, add, path) != "A":
        problems.append(f"{path}: its first commit {_short(add)} does not add it")
    frozen_blob = blob_id(root, add, path)
    if blob_id(root, "HEAD", path) != frozen_blob:
        problems.append(f"{path}: content at HEAD differs from its freezing commit {_short(add)}")
    if blob_id(root, "", path) != frozen_blob:
        problems.append(f"{path}: staged changes to a frozen preregistration")
    if (root / path).is_file() and hash_object(root, root / path) != frozen_blob:
        problems.append(f"{path}: modified in the working tree since it was frozen")
    return add


def _collect_preregs(root: Path, problems: list[str]) -> dict[str, Prereg]:
    """Check every preregistration file; return the tracked, well-named ones."""
    tracked = set(tracked_files(root, PREREG_DIR))
    for path in untracked_files(root, PREREG_DIR):
        if path in META_FILES:
            continue
        problems.append(f"{path}: not committed (a preregistration is frozen by its commit)")
        text = _read(root, path)
        if NAME_RE.match(path) and text is not None:
            problems.extend(f"{path}: {p}" for p in check_document(text)[2])
    for path in sorted(set(history_paths(root, PREREG_DIR)) - tracked - META_FILES):
        problems.append(f"{path}: deleted or renamed after it was frozen")
    preregs: dict[str, Prereg] = {}
    for path in sorted(tracked - META_FILES):
        if not NAME_RE.match(path):
            problems.append(f"{path}: unexpected file; preregistrations are prereg/NNNN-name.md")
            continue
        prereg = Prereg(path=path, add_commit=_check_frozen(root, path, problems))
        text = _read(root, path)
        if text is None:
            problems.append(f"{path}: missing from the working tree or not UTF-8")
        else:
            prereg.front, prereg.config_sha256, found = check_document(text)
            problems.extend(f"{path}: {p}" for p in found)
        preregs[path] = prereg
    _check_unique(preregs, problems)
    return preregs


def _check_unique(preregs: dict[str, Prereg], problems: list[str]) -> None:
    """Numbers (NNNN) and ids must not repeat across preregistrations."""
    by_number: dict[str, list[str]] = {}
    by_id: dict[str, list[str]] = {}
    for path, prereg in preregs.items():
        m = NAME_RE.match(path)
        assert m is not None
        by_number.setdefault(m.group(1), []).append(path)
        if prereg.front.get("id"):
            by_id.setdefault(prereg.front["id"], []).append(path)
    for number, paths in sorted(by_number.items()):
        if len(paths) > 1:
            problems.append(f"preregistration number {number} is used by {', '.join(paths)}")
    for pid, paths in sorted(by_id.items()):
        if len(paths) > 1:
            problems.append(f"preregistration id {pid!r} is used by {', '.join(paths)}")


def _check_amendments(root: Path, preregs: dict[str, Prereg], problems: list[str]) -> None:
    """An amendment must cite an existing preregistration committed strictly earlier."""
    for path, prereg in sorted(preregs.items()):
        target = prereg.front.get("amends")
        if target is None:
            continue
        original = preregs.get(target)
        if target == path or original is None:
            problems.append(f"{path}: amends {target!r}, which is not another preregistration")
            continue
        if prereg.add_commit is None or original.add_commit is None:
            problems.append(f"{path}: cannot order it against {target}: commit unknown")
        elif not is_strict_ancestor(root, original.add_commit, prereg.add_commit):
            problems.append(
                f"{path}: amends {target}, which was not committed strictly earlier "
                f"({_short(original.add_commit)} vs {_short(prereg.add_commit)})"
            )


def _check_artifacts(root: Path, preregs: dict[str, Prereg], problems: list[str]) -> int:
    """Check every tracked artifact JSON that cites a preregistration.

    Returns:
        The number of artifacts that cite a preregistration.
    """
    linked = 0
    for art in tracked_files(root, "artifacts/"):
        if not art.endswith(".json"):
            continue
        text = _read(root, art)
        if text is None:
            problems.append(f"{art}: missing from the working tree or not UTF-8")
            continue
        try:
            data = json.loads(text)
        except ValueError as exc:
            problems.append(f"{art}: not valid JSON ({exc})")
            continue
        if not isinstance(data, dict):
            continue
        if "prereg" not in data:
            if data.get("confirmatory") is True:
                problems.append(f"{art}: confirmatory but cites no preregistration ('prereg')")
            continue
        linked += 1
        cited = data["prereg"]
        prereg = preregs.get(cited) if isinstance(cited, str) else None
        if prereg is None:
            problems.append(f"{art}: cites {cited!r}, which is not a tracked prereg/NNNN-*.md")
            continue
        if prereg.config_sha256 is None:
            problems.append(f"{art}: cites {cited}, whose prereg-config block is invalid")
        elif data.get("prereg_config_sha256") != prereg.config_sha256:
            problems.append(
                f"{art}: prereg_config_sha256 {data.get('prereg_config_sha256')!r} does not "
                f"match {cited} ({prereg.config_sha256})"
            )
        commits = set(commits_touching(root, art)) | set(commits_following(root, art))
        if not commits:
            problems.append(f"{art}: not committed")
        elif prereg.add_commit is None:
            problems.append(f"{art}: cannot order it against {cited}: commit unknown")
        else:
            early = sorted(c for c in commits if not is_strict_ancestor(root, prereg.add_commit, c))
            if early:
                problems.append(
                    f"{art}: committed in {', '.join(_short(c) for c in early)}, not strictly "
                    f"after {cited} was frozen in {_short(prereg.add_commit)}"
                )
    return linked


def verify(root: Path) -> tuple[list[str], int, int]:
    """Verify every preregistration and every artifact that cites one.

    Args:
        root: Any directory inside the repository's work tree.

    Returns:
        (problems, number of preregistrations, number of linked artifacts).
    """
    problems: list[str] = []
    try:
        top = repo_toplevel(root)
        preregs = _collect_preregs(top, problems)
        if not preregs:
            problems.append("no preregistrations under prereg/")
        _check_amendments(top, preregs, problems)
        linked = _check_artifacts(top, preregs, problems)
    except GitError as exc:
        problems.append(f"git history unavailable: {exc}")
        return problems, 0, 0
    return problems, len(preregs), linked


def cmd_verify(root: Path) -> int:
    """Print the verification report; return the exit code."""
    problems, n, m = verify(root)
    for problem in problems:
        print(f"- {problem}")
    if problems:
        print(f"PREREG-VERIFY: FAIL {len(problems)}")
        return 1
    print(f"PREREG-VERIFY: PASS {n} preregistrations, {m} linked artifacts")
    return 0


def cmd_hash(root: Path, path: str) -> int:
    """Validate one preregistration file and print its config hash."""
    target = Path(path) if Path(path).is_absolute() else root / path
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"prereg hash: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    _, digest, problems = check_document(text)
    for problem in problems:
        print(f"prereg hash: {path}: {problem}", file=sys.stderr)
    if problems or digest is None:
        return 1
    print(digest)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    root_help = "repository root (default: the current directory)"
    parser.add_argument("--root", type=Path, default=None, help=root_help)
    sub = parser.add_subparsers(dest="command", required=True)
    p_verify = sub.add_parser("verify", help="check preregistrations and the artifacts citing them")
    p_hash = sub.add_parser("hash", help="validate one file and print its canonical config hash")
    p_hash.add_argument("path")
    for p in (p_verify, p_hash):
        p.add_argument("--root", type=Path, default=argparse.SUPPRESS, help=root_help)
    args = parser.parse_args(argv)
    root = (args.root or Path.cwd()).resolve()
    if args.command == "hash":
        return cmd_hash(root, args.path)
    return cmd_verify(root)


if __name__ == "__main__":
    sys.exit(main())
