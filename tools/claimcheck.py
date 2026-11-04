#!/usr/bin/env python3
# Ported from tools/claimcheck.py of https://github.com/raj200501/raise-v1-public
# at commit 9330c9d396b328cef4736356e0c8962218f0ecd2, Copyright 2026 Raj Kashikar,
# licensed under the Apache License, Version 2.0 (third_party/raise-v1/LICENSE and
# third_party/raise-v1/NOTICE). Changed for base-rate: instead of accepting a number
# when any banked value rounds to it, a document must be its template rendered with
# each claim taken from a committed version of that claim's own source, and every
# other number must carry an allowance tag in the template (tools/claims.py).
"""Claim integrity gate (``make claimcheck``, gate G10, non-negotiable 1).

Rule enforced: every number in a results document is either a claim rendered
from a banked artifact or explicitly allowed non-result text. For each
document (default: every document in docs/claims.json):

1. The document, its template, and the registry exist, and the document is
   registered there.
2. Every number in the template's literal text is an identifier under the
   principled exclusions of tools/claims.py (URLs, link targets, digests,
   repository paths, list markers, numbered headings, section or table
   references) or sits inside an allowance tag that passes its rule.
3. Every ``{{claim:<id>}}`` names a registered, valid claim whose source is a
   tracked, committed file under artifacts/ (or a preregistration's config).
4. The document equals its template with each claim replaced by the claim's
   rendering from some committed version of its source. A hand-edited number
   or sentence fails here. A value that was banked but is no longer current
   passes here and fails tools/freshness.py.

Usage: ``python3 tools/claimcheck.py [--root DIR] [DOC ...]``. The last stdout
line is ``CLAIMCHECK: PASS ...`` or ``CLAIMCHECK: FAIL <n> problems``. Exit 0
only on PASS. Stdlib only.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from chainlib import GitError  # noqa: E402
from claims import (  # noqa: E402
    ClaimError,
    Mismatch,
    Registry,
    Template,
    Workspace,
    allowance_problem,
    decompose,
    describe_mismatch,
    doc_rel,
    find_numbers,
    line_col,
    options_ends,
    resolve_root,
    wildcard_ends,
)


@dataclass
class Stats:
    """Counts for the PASS line."""

    documents: int = 0
    claim_spans: int = 0
    allowances: int = 0
    sources: set[str] = field(default_factory=set)


def _template_problems(ws: Workspace, template: Template) -> tuple[list[str], int]:
    """Syntax, literal-number, and allowance problems of a template."""
    out = [f"{template.path}:{line}: {msg}" for line, msg in template.problems]
    top_dirs = ws.top_dirs()
    for offset, token in find_numbers(template.literal_view(), top_dirs):
        out.append(
            f"{template.path}:{template.line_of(offset)}: number {token!r} is outside any "
            f"claim or allowance | {template.line_text(offset)}"
        )
    allowed = 0
    for p in template.placeholders:
        if p.kind == "claim":
            continue
        problem = allowance_problem(p.kind, p.text, ws.brief)
        if problem:
            tag = "{{" + p.kind + ":" + p.text + "}}"
            out.append(f"{template.path}:{template.line_of(p.start)}: {tag} {problem}")
        else:
            allowed += 1
    return out, allowed


def _claim_options(
    ws: Workspace, registry: Registry, template: Template, stats: Stats
) -> tuple[dict[str, list[str]], list[str]]:
    """Banked renderings of every claim the template uses."""
    options: dict[str, list[str]] = {}
    out: list[str] = []
    for p in template.claims():
        if p.text in options:
            continue
        where = f"{template.path}:{template.line_of(p.start)}"
        claim = registry.claims.get(p.text)
        if claim is None:
            state = "invalid" if p.text in registry.invalid else "not registered"
            out.append(f"{where}: claim {p.text!r} is {state} in {registry.path}")
            options[p.text] = []
            continue
        try:
            options[p.text] = ws.banked(claim)
            stats.sources.add(claim.source)
        except (ClaimError, GitError) as exc:
            out.append(f"{where}: claim {p.text}: {exc}")
            options[p.text] = []
    return options, out


def _hint(ws: Workspace, registry: Registry, cid: str, span: str) -> str:
    """Say so when the value is in the work tree but not yet committed."""
    try:
        if ws.current(registry.claims[cid]) == span:
            return "; the work-tree source gives this value: commit it to bank it"
    except (ClaimError, KeyError):
        pass
    return ""


def check_document(ws: Workspace, registry: Registry, doc: str, stats: Stats) -> list[str]:
    """All claimcheck problems of one document (empty means it passes)."""
    path = ws.root / doc
    if not path.is_file():
        return [f"{doc}: file not found"]
    try:
        template = ws.template_for(doc)
    except ClaimError as exc:
        return [f"{doc}: {exc}"]
    text = path.read_text(encoding="utf-8")
    out, allowed = _template_problems(ws, template)
    options, claim_problems = _claim_options(ws, registry, template, stats)
    out.extend(claim_problems)
    pieces = template.pieces()
    match = decompose(text, pieces, options_ends(text, options))
    if isinstance(match, Mismatch):
        wild = decompose(text, pieces, wildcard_ends(text))
        if isinstance(wild, Mismatch):
            out.append(describe_mismatch(doc, text, template, pieces, wild))
        else:
            for piece, (start, end) in zip(pieces, wild, strict=True):
                span = text[start:end]
                if not piece.claim or span in options.get(piece.text, ()):
                    continue
                if not options.get(piece.text):
                    continue  # already reported: the claim has no banked rendering
                line, _ = line_col(text, start)
                banked = ", ".join(repr(o) for o in options[piece.text][:4])
                hint = _hint(ws, registry, piece.text, span)
                out.append(
                    f"{doc}:{line}: {span!r} for claim {piece.text} is not a rendering of any "
                    f"committed version of its source (banked: {banked}){hint}"
                )
    if not out:
        stats.documents += 1
        stats.claim_spans += len(template.claims())
        stats.allowances += allowed
    return out


def check(ws: Workspace, docs: Sequence[str]) -> tuple[list[str], Stats]:
    """Check documents (default: all registered); return problems and counts."""
    stats = Stats()
    try:
        registry = ws.registry()
    except ClaimError as exc:
        missing = [
            f"{d}: file not found" for d in dict.fromkeys(docs) if not (ws.root / d).is_file()
        ]
        return [*missing, str(exc)], stats
    problems = list(registry.problems)
    targets = list(dict.fromkeys(docs)) or sorted(registry.documents)
    if not targets:
        problems.append(f"{registry.path} registers no documents")
    for doc in targets:
        try:
            problems.extend(check_document(ws, registry, doc, stats))
        except GitError as exc:
            problems.append(f"{doc}: git history unavailable: {exc}")
    return problems, stats


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("docs", nargs="*", help="documents (default: all in docs/claims.json)")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    parser.add_argument("--registry", default="docs/claims.json", help="registry path")
    args = parser.parse_args(argv)
    root = resolve_root(args.root)
    ws = Workspace(root, args.registry)
    problems, stats = check(ws, [doc_rel(root, d) for d in args.docs])
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        print(f"CLAIMCHECK: FAIL {len(problems)} problems")
        return 1
    print(
        f"CLAIMCHECK: PASS {stats.documents} documents, {stats.claim_spans} claim spans from "
        f"{len(stats.sources)} banked sources, {stats.allowances} allowances"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
