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
- A preregistration is frozen by its commit: normally exactly one ordinary
  commit touches it (the one that adds it), and it is never modified,
  renamed, or deleted afterwards. Its content at HEAD, in the index, and in
  the working tree equals the frozen content.
- Draft stage (DECISIONS.md D19): a file whose config holds a string value
  ``TO_BE_SET`` or ``TO_BE_SET_<WORDS>`` declares itself an unfinished draft.
  It is frozen by the first commit whose config holds no such value; every
  earlier commit must hold one, and the freezing commit may change the config
  only where a draft held a placeholder. A file still holding a placeholder
  at HEAD fails. Everything below counts from the freezing commit.
- An amendment names the original in front matter (``amends: prereg/....md``);
  the original must exist and be frozen strictly before the amendment.
- Every tracked JSON file under ``artifacts/`` with a ``prereg`` field cites
  an existing preregistration, carries ``prereg_config_sha256`` equal to the
  SHA-256 of the canonical JSON of that preregistration's config block, and
  every commit that touched the artifact comes strictly after the commit that
  froze the preregistration (not merely after a draft of it). An artifact
  with ``"confirmatory": true`` must cite one. Exploratory artifacts omit the
  ``prereg`` field.

Commands:
  verify [--root DIR]       check all of the above using DIR's git history
  hash PATH [--root DIR]    validate PATH and print its config hash

The last stdout line of ``verify`` is ``PREREG-VERIFY: PASS <n> preregistrations,
<m> linked artifacts`` or ``PREREG-VERIFY: FAIL <reason count>`` with the reasons
above it (``- `` lines) and a ``note: `` line for each file frozen after a
draft. Exit 0 only on PASS. Stdlib only.
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
    REGULAR_MODES,
    GitError,
    blob_bytes,
    blob_id,
    descendants,
    hash_object,
    history,
    history_paths,
    introducing,
    is_shallow,
    is_strict_ancestor,
    path_states,
    rename_sources,
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
# A config string value that marks the file as an unfinished draft (D19).
PLACEHOLDER_RE = re.compile(r"TO_BE_SET(?:_[A-Z0-9]+)*")
"""Matched with ``fullmatch``: a trailing newline or any other text is not a placeholder."""


class PreregError(ValueError):
    """A preregistration file does not meet the contract."""


@dataclass
class Prereg:
    """One preregistration file as read from the working tree."""

    path: str
    front: dict[str, str] = field(default_factory=dict)
    config_sha256: str | None = None
    freeze_commit: str | None = None
    drafts: list[str] = field(default_factory=list)
    first_commit: str | None = None
    introduced: list[str] = field(default_factory=list)


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


def placeholder_paths(config: Any, at: str = "") -> list[str]:
    """JSON paths (``/key/0`` style) of every draft placeholder in ``config``."""
    if isinstance(config, str):
        return [at or "/"] if PLACEHOLDER_RE.fullmatch(config) else []
    if isinstance(config, dict):
        return [p for k, v in config.items() for p in placeholder_paths(v, f"{at}/{k}")]
    if isinstance(config, list):
        return [p for i, v in enumerate(config) for p in placeholder_paths(v, f"{at}/{i}")]
    return []


def fills_only_placeholders(draft: Any, final: Any) -> bool:
    """Whether ``final`` equals ``draft`` except where ``draft`` holds a placeholder.

    Objects must keep the same keys and lists the same length; a placeholder
    may be replaced by any value, and every other value must stay equal and of
    the same JSON type (``1`` is not ``1.0`` or ``true``).
    """
    if isinstance(draft, str) and PLACEHOLDER_RE.fullmatch(draft):
        return True
    if isinstance(draft, dict):
        return (
            isinstance(final, dict)
            and draft.keys() == final.keys()
            and all(fills_only_placeholders(draft[k], final[k]) for k in draft)
        )
    if isinstance(draft, list):
        return (
            isinstance(final, list)
            and len(draft) == len(final)
            and all(fills_only_placeholders(d, f) for d, f in zip(draft, final, strict=True))
        )
    return type(draft) is type(final) and bool(draft == final)


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
        config = parse_config(text)
        digest: str | None = sha256_obj(config)
    except PreregError as exc:
        digest = None
        problems.append(str(exc))
    else:
        held = placeholder_paths(config)
        if held:
            problems.append(
                f"still a draft: its config holds a TO_BE_SET placeholder at {', '.join(held)}"
            )
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


def _config_of(root: Path, state: tuple[str, str] | None) -> dict[str, Any] | None:
    """The config block of one committed version (CRLF read as LF), or None if invalid."""
    if state is None or state[1] not in REGULAR_MODES:
        return None
    try:
        text = blob_bytes(root, state[0]).decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        return parse_config(text.replace("\r\n", "\n").replace("\r", "\n"))
    except PreregError:
        return None


@dataclass
class History:
    """HEAD's history, computed once per verification."""

    order: list[str]
    parents: dict[str, tuple[str, ...]]
    after: dict[str, frozenset[str]] = field(default_factory=dict)

    def descendants(self, root: Path, commit: str) -> frozenset[str]:
        """Strict descendants of ``commit`` in HEAD's history (cached)."""
        if commit not in self.after:
            self.after[commit] = descendants(root, commit)
        return self.after[commit]


def _check_frozen(
    root: Path, path: str, hist: History, problems: list[str]
) -> tuple[str | None, list[str], list[str]]:
    """Check that one tracked preregistration is unchanged since it was frozen.

    Every version of the file in HEAD's history counts, merge commits
    included. The freezing commit is the first commit that introduces a
    version whose config holds no draft placeholder; it must be an ordinary
    commit. Versions introduced before it are drafts (module docstring); no
    version may be introduced after it, and every commit that descends from it
    holds exactly the frozen version.

    Returns:
        (freezing commit or None, draft commits oldest first, every commit that
        introduced a version, oldest first).
    """
    states = path_states(root, path, hist.order, hist.parents)
    intro = introducing(hist.order, hist.parents, states)
    if not intro:
        problems.append(f"{path}: tracked but not added by any commit")
        return None, [], []
    for commit in intro:
        state = states[commit]
        if state is not None and state[1] not in REGULAR_MODES:
            problems.append(f"{path}: not a regular file in {_short(commit)}")
    configs = {c: _config_of(root, states[c]) for c in intro}
    drafted = {c: bool(configs[c] and placeholder_paths(configs[c])) for c in intro}
    finals = [c for c in intro if not drafted[c]]
    if not finals:
        held = ", ".join(placeholder_paths(configs[intro[-1]]))
        problems.append(
            f"{path}: still a draft: its config holds a TO_BE_SET placeholder at {held} "
            f"in {_short(intro[-1])}; a preregistration must be frozen"
        )
        return None, intro, intro
    freeze = finals[0]
    pos = {c: i for i, c in enumerate(hist.order)}
    drafts = [c for c in intro if pos[c] < pos[freeze]]
    later = [c for c in intro if pos[c] > pos[freeze]]
    if len(hist.parents[freeze]) > 1:
        problems.append(
            f"{path}: frozen in merge commit {_short(freeze)}; a preregistration must be "
            "frozen by an ordinary commit"
        )
    if later:
        shas = ", ".join(_short(c) for c in intro)
        problems.append(
            f"{path}: changed after it was frozen; touched by {len(intro)} commits ({shas}); "
            f"frozen in {_short(freeze)}"
        )
    else:
        moved = sorted(c for c in hist.descendants(root, freeze) if states.get(c) != states[freeze])
        if moved:
            problems.append(
                f"{path}: changed after it was frozen in {_short(freeze)}: commits "
                f"{', '.join(_short(c) for c in moved[:3])} hold other content"
            )
    final = configs[freeze]
    for draft in drafts:
        if not is_strict_ancestor(root, draft, freeze):
            problems.append(
                f"{path}: draft {_short(draft)} does not precede the freezing commit "
                f"{_short(freeze)}"
            )
        if final is None or not fills_only_placeholders(configs[draft], final):
            problems.append(
                f"{path}: freezing commit {_short(freeze)} changes a config value that draft "
                f"{_short(draft)} had fixed; only its placeholders may change"
            )
    frozen_state = states[freeze]
    frozen_blob = frozen_state[0] if frozen_state else None
    if blob_id(root, "HEAD", path) != frozen_blob:
        problems.append(
            f"{path}: content at HEAD differs from its freezing commit {_short(freeze)}"
        )
    if blob_id(root, "", path) != frozen_blob:
        problems.append(f"{path}: staged changes to a frozen preregistration")
    if (root / path).is_file() and hash_object(root, root / path) != frozen_blob:
        problems.append(f"{path}: modified in the working tree since it was frozen")
    return freeze, drafts, intro


def _collect_preregs(root: Path, hist: History, problems: list[str]) -> dict[str, Prereg]:
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
        freeze, drafts, intro = _check_frozen(root, path, hist, problems)
        prereg = Prereg(
            path=path,
            freeze_commit=freeze,
            drafts=drafts,
            first_commit=intro[0] if intro else None,
            introduced=intro,
        )
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


def _check_amendments(
    root: Path, hist: History, preregs: dict[str, Prereg], problems: list[str]
) -> None:
    """An amendment must cite a preregistration frozen strictly before the amendment's
    first commit, so not even a draft of the amendment predates the original's freeze."""
    for path, prereg in sorted(preregs.items()):
        target = prereg.front.get("amends")
        if target is None:
            continue
        original = preregs.get(target)
        if target == path or original is None:
            problems.append(f"{path}: amends {target!r}, which is not another preregistration")
            continue
        if not prereg.introduced or original.freeze_commit is None:
            problems.append(f"{path}: cannot order it against {target}: commit unknown")
            continue
        after = hist.descendants(root, original.freeze_commit)
        early = [c for c in prereg.introduced if c not in after]
        if early:
            problems.append(
                f"{path}: amends {target}, which was not committed strictly earlier "
                f"({_short(original.freeze_commit)} vs {', '.join(_short(c) for c in early)})"
            )


def _check_artifacts(
    root: Path, hist: History, preregs: dict[str, Prereg], problems: list[str]
) -> int:
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
        present: set[str] = set()
        for name in rename_sources(root, art):
            states = path_states(root, name, hist.order, hist.parents)
            present |= {c for c, state in states.items() if state is not None}
        if not present:
            problems.append(f"{art}: not committed")
        elif prereg.freeze_commit is None:
            problems.append(f"{art}: cannot order it against {cited}: commit unknown")
        else:
            frozen = prereg.freeze_commit
            after = hist.descendants(root, frozen)
            pos = {c: i for i, c in enumerate(hist.order)}
            early = sorted((c for c in present if c not in after), key=pos.__getitem__)
            if early:
                shown = ", ".join(_short(c) for c in early[:3])
                more = f" and {len(early) - 3} more" if len(early) > 3 else ""
                problems.append(
                    f"{art}: committed in {shown}{more}, not strictly after {cited} was frozen "
                    f"in {_short(frozen)}"
                )
    return linked


def verify(root: Path, notes: list[str] | None = None) -> tuple[list[str], int, int]:
    """Verify every preregistration and every artifact that cites one.

    Args:
        root: Any directory inside the repository's work tree.
        notes: If given, receives one line per preregistration that was
            frozen after a draft stage (D19), naming both commits.

    Returns:
        (problems, number of preregistrations, number of linked artifacts).
    """
    problems: list[str] = []
    try:
        top = repo_toplevel(root)
        if is_shallow(top):
            problems.append(
                "git history is shallow, so commit order cannot be checked; "
                "run git fetch --unshallow"
            )
            return problems, 0, 0
        hist = History(*history(top))
        preregs = _collect_preregs(top, hist, problems)
        if not preregs:
            problems.append("no preregistrations under prereg/")
        _check_amendments(top, hist, preregs, problems)
        linked = _check_artifacts(top, hist, preregs, problems)
    except GitError as exc:
        problems.append(f"git history unavailable: {exc}")
        return problems, 0, 0
    if notes is not None:
        for path, prereg in sorted(preregs.items()):
            if prereg.drafts and prereg.freeze_commit is not None:
                drafts = ", ".join(_short(c) for c in prereg.drafts)
                notes.append(
                    f"{path}: frozen in {_short(prereg.freeze_commit)} after draft "
                    f"commit(s) {drafts} that held a TO_BE_SET placeholder"
                )
    return problems, len(preregs), linked


def cmd_verify(root: Path) -> int:
    """Print the verification report; return the exit code."""
    notes: list[str] = []
    problems, n, m = verify(root, notes)
    for note in notes:
        print(f"note: {note}")
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
