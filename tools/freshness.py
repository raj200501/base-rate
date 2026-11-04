#!/usr/bin/env python3
# Ported from tools/freshness.py of https://github.com/raj200501/raise-v1-public
# at commit 9330c9d396b328cef4736356e0c8962218f0ecd2, Copyright 2026 Raj Kashikar,
# licensed under the Apache License, Version 2.0 (third_party/raise-v1/LICENSE and
# third_party/raise-v1/NOTICE). Changed for base-rate: claims are the placeholders of
# a document's template (registered in docs/claims.json) rather than regex patterns
# over the document, so a claim cannot drop out of the check by being reworded; the
# path grammar gained list filters and a format step (tools/claims.py).
"""Freshness gate (``make freshness``, gate G10): every live claim is current.

Why this is separate from tools/claimcheck.py: claimcheck accepts a claim
value if some committed version of its source gives it. That is the right
test for a fabricated number and the wrong one for a stale number: a value
that was true when it was written stays in git history forever, so it keeps
passing after the artifact moves on. This gate re-derives every claim from
its source as it is now and fails on any difference.

For each document (default: every document in docs/claims.json) it renders
the template with the current value of every claim and requires the document
to equal that rendering. On a difference it reports each stale claim with
the written and the current value, or the line where the document departs
from its template. A claim whose source is missing or whose extractor no
longer resolves is a failure, never a skip.

Usage: ``python3 tools/freshness.py [--root DIR] [DOC ...]``. The last stdout
line is ``FRESHNESS: PASS ...`` or ``FRESHNESS: FAIL <n> ...``. Exit 0 only on
PASS. Stdlib only.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from claims import (  # noqa: E402
    ClaimError,
    Mismatch,
    Registry,
    Workspace,
    decompose,
    describe_mismatch,
    doc_rel,
    line_col,
    resolve_root,
    wildcard_ends,
)


def check_document(ws: Workspace, registry: Registry, doc: str) -> tuple[list[str], int]:
    """Stale or unresolvable claims of one document, and its live-claim count."""
    path = ws.root / doc
    if not path.is_file():
        return [f"{doc}: file not found"], 0
    try:
        template = ws.template_for(doc)
    except ClaimError as exc:
        return [f"{doc}: {exc}"], 0
    out = [f"{template.path}:{line}: {msg}" for line, msg in template.problems]
    values: dict[str, str] = {}
    for p in template.claims():
        if p.text in values:
            continue
        where = f"{template.path}:{template.line_of(p.start)}"
        claim = registry.claims.get(p.text)
        if claim is None:
            state = "invalid" if p.text in registry.invalid else "not registered"
            out.append(f"{where}: claim {p.text!r} is {state} in {registry.path}")
            continue
        try:
            values[p.text] = ws.current(claim)
        except ClaimError as exc:
            out.append(f"{where}: claim {p.text}: {exc}")
    if out:
        return out, 0
    text = path.read_text(encoding="utf-8")
    if text == template.render(values):
        return [], len(template.claims())
    pieces = template.pieces()
    wild = decompose(text, pieces, wildcard_ends(text))
    if isinstance(wild, Mismatch):
        return [describe_mismatch(doc, text, template, pieces, wild)], 0
    for piece, (start, end) in zip(pieces, wild, strict=True):
        span = text[start:end]
        if piece.claim and span != values[piece.text]:
            line, _ = line_col(text, start)
            source = registry.claims[piece.text].source
            out.append(
                f"{doc}:{line}: claim {piece.text} is stale: the document says {span!r}, "
                f"{source} now gives {values[piece.text]!r}"
            )
    return out or [f"{doc}: differs from its rendering; run python3 tools/claims.py render"], 0


def check(ws: Workspace, docs: Sequence[str]) -> tuple[list[str], int, int]:
    """Check documents; return (problems, live claims checked, documents)."""
    try:
        registry = ws.registry()
    except ClaimError as exc:
        missing = [
            f"{d}: file not found" for d in dict.fromkeys(docs) if not (ws.root / d).is_file()
        ]
        return [*missing, str(exc)], 0, 0
    problems = list(registry.problems)
    targets = list(dict.fromkeys(docs)) or sorted(registry.documents)
    if not targets:
        problems.append(f"{registry.path} registers no documents")
    live = 0
    for doc in targets:
        found, n = check_document(ws, registry, doc)
        problems.extend(found)
        live += n
    return problems, live, len(targets)


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
    problems, live, n_docs = check(
        Workspace(root, args.registry), [doc_rel(root, d) for d in args.docs]
    )
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        print(f"FRESHNESS: FAIL {len(problems)} stale or unresolvable")
        return 1
    print(f"FRESHNESS: PASS {live} live claims current in {n_docs} documents")
    return 0


if __name__ == "__main__":
    sys.exit(main())
