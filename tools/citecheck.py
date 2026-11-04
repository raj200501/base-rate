"""Offline validator for ``make citecheck`` (brief rule 11, gate G13).

Reads ``artifacts/citecheck.json`` (schema ``baserate-citecheck/1``), validates
every entry, and checks that every citation key used in the documents is
present and verified. Stdlib only, no network.

Citation convention: a document cites a reference by writing ``[@key]`` where
``key`` matches ``[A-Za-z0-9][A-Za-z0-9_.-]*`` and equals the ``key`` field of
an entry in the citecheck file. Fenced code blocks and inline code spans are
ignored, so the convention can be shown without being a citation.

Documents checked: ``README.md`` and ``docs/REPORT.md`` (both required), plus
every ``*.md`` file under ``docs/templates/`` when that directory exists.

A key used in a document but missing, malformed, or unverified is a failure.
Verified entries that no document cites are fine. If no document cites
anything the check fails, because gate G13 needs at least one citation.

Output: one line per problem, then exactly ``CITECHECK: PASS n/n verified``
(n is the number of distinct cited keys) or ``CITECHECK: FAIL v/n verified``.
Exit status 0 on pass, 1 on fail.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = "baserate-citecheck/1"
CITATION_FILE = Path("artifacts") / "citecheck.json"
REQUIRED_DOCS = (Path("README.md"), Path("docs") / "REPORT.md")
TEMPLATE_DIR = Path("docs") / "templates"

CITE_RE = re.compile(r"\[@([A-Za-z0-9][A-Za-z0-9_.-]*)\]")
FENCE_RE = re.compile(r"^(`{3,}|~{3,}).*?^\1[ \t]*$", re.S | re.M)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def strip_code(text: str) -> str:
    """Remove fenced code blocks and inline code spans from Markdown text."""
    text = FENCE_RE.sub("", text)
    return INLINE_CODE_RE.sub("", text)


def cited_keys(text: str) -> list[str]:
    """Return citation keys written as ``[@key]`` outside code, in order."""
    return CITE_RE.findall(strip_code(text))


def _is_iso(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_entry(entry: object, index: int) -> tuple[str | None, list[str]]:
    """Validate one reference entry.

    Returns:
        The entry key (None when unusable) and a list of problem lines.
    """
    where = f"entry {index}"
    if not isinstance(entry, dict):
        return None, [f"{where}: not an object"]
    e: dict[str, Any] = entry
    key = e.get("key")
    if not _nonempty_str(key):
        return None, [f"{where}: missing or empty key"]
    assert isinstance(key, str)
    where = f"entry {key}"
    problems: list[str] = []
    if not CITE_RE.fullmatch(f"[@{key}]"):
        problems.append(f"{where}: key is not usable as [@key]")
    claimed = e.get("claimed")
    if not isinstance(claimed, dict):
        problems.append(f"{where}: claimed is not an object")
    else:
        if not _nonempty_str(claimed.get("title")):
            problems.append(f"{where}: claimed.title missing")
        url = claimed.get("url")
        if not (_nonempty_str(url) and str(url).startswith(("http://", "https://"))):
            problems.append(f"{where}: claimed.url missing or not http(s)")
        year = claimed.get("year")
        if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
            problems.append(f"{where}: claimed.year must be an integer or null")
        authors = claimed.get("authors")
        if authors is not None and not isinstance(authors, str):
            problems.append(f"{where}: claimed.authors must be a string or null")
    if not _is_iso(e.get("retrieved_at")):
        problems.append(f"{where}: retrieved_at is not an ISO 8601 date")
    final_url = e.get("final_url")
    if not _nonempty_str(final_url):
        problems.append(f"{where}: final_url missing")
    page_title = e.get("page_title")
    if page_title is not None and not isinstance(page_title, str):
        problems.append(f"{where}: page_title must be a string or null")
    sha = e.get("content_sha256")
    if sha is not None and not (isinstance(sha, str) and SHA256_RE.match(sha)):
        problems.append(f"{where}: content_sha256 must be null or 64 lowercase hex characters")
    verified = e.get("verified")
    if not isinstance(verified, bool):
        problems.append(f"{where}: verified must be a boolean")
    elif verified and not _nonempty_str(page_title):
        problems.append(f"{where}: verified is true but page_title is empty")
    if not _nonempty_str(e.get("notes")):
        problems.append(f"{where}: notes missing")
    return key, problems


def load_entries(path: Path) -> tuple[dict[str, bool], list[str]]:
    """Load the citecheck file.

    Returns:
        A map from key to "entry is well formed and verified", and problems.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, [f"{path}: file not found"]
    except (OSError, ValueError) as exc:
        return {}, [f"{path}: cannot read or parse ({exc})"]
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return {}, [f"{path}: schema must be {SCHEMA}"]
    refs = data.get("references")
    if not isinstance(refs, list):
        return {}, [f"{path}: references must be a list"]
    status: dict[str, bool] = {}
    problems: list[str] = []
    for i, entry in enumerate(refs):
        key, issues = validate_entry(entry, i)
        problems.extend(issues)
        if key is None:
            continue
        if key in status:
            problems.append(f"entry {key}: duplicate key")
            status[key] = False
            continue
        ok = not issues and isinstance(entry, dict) and entry.get("verified") is True
        status[key] = ok
    return status, problems


def document_paths(root: Path) -> list[Path]:
    """Return the documents to scan: required ones plus any templates."""
    paths = [root / p for p in REQUIRED_DOCS]
    tdir = root / TEMPLATE_DIR
    if tdir.is_dir():
        paths.extend(sorted(tdir.rglob("*.md")))
    return paths


def run(root: Path) -> tuple[list[str], int, int]:
    """Run the check.

    Returns:
        Problem lines, number of cited keys verified, number of distinct keys cited.
    """
    status, problems = load_entries(root / CITATION_FILE)
    cited: dict[str, list[str]] = {}
    for path in document_paths(root):
        rel = path.relative_to(root).as_posix()
        if not path.is_file():
            problems.append(f"{rel}: document missing")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{rel}: cannot read ({exc})")
            continue
        for key in cited_keys(text):
            cited.setdefault(key, [])
            if rel not in cited[key]:
                cited[key].append(rel)
    verified = 0
    for key in sorted(cited):
        where = ", ".join(cited[key])
        if key not in status:
            problems.append(f"{key}: cited in {where} but not in {CITATION_FILE.as_posix()}")
        elif not status[key]:
            problems.append(f"{key}: cited in {where} but not verified or entry malformed")
        else:
            verified += 1
    if not cited:
        problems.append("no document cites any reference with [@key]; gate G13 needs at least one")
    return problems, verified, len(cited)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``make citecheck``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    args = parser.parse_args(argv)
    problems, verified, total = run(args.root)
    for line in problems:
        print(line)
    ok = not problems and verified == total and total > 0
    print(f"CITECHECK: {'PASS' if ok else 'FAIL'} {verified}/{total} verified")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
