#!/usr/bin/env python3
# Adapted from tools/claimcheck.py and tools/freshness.py of
# https://github.com/raj200501/raise-v1-public at commit
# 9330c9d396b328cef4736356e0c8962218f0ecd2, Copyright 2026 Raj Kashikar, licensed under
# the Apache License, Version 2.0 (third_party/raise-v1/LICENSE and
# third_party/raise-v1/NOTICE). Taken from there: the number pattern, the principled
# exclusions (URLs, digests, list markers, drawing geometry), and the small auditable
# path grammar of freshness.py. Changed for base-rate: results documents are rendered
# from templates whose numbers are named claims or tagged allowances, instead of being
# matched against every value in artifacts/ and a separate allow-list file.
"""Claims: results documents rendered from templates bound to banked artifacts.

docs/CLAIMS.md is the guide for authors. In short:

- ``docs/claims.json`` (the registry) maps each results document to its
  template (``documents``) and each claim id to a source file plus a
  deterministic extractor (``claims``: ``source``, ``select``, ``format``).
- A template is the document's source. A result number appears in it only as
  ``{{claim:<id>}}``. Any other number must sit inside an allowance tag that
  names why it is not a result: ``{{param:...}}``, ``{{threshold:...}}``,
  ``{{cap:...}}``, ``{{date:...}}``, ``{{version:...}}``, ``{{label:...}}``,
  ``{{name:...}}``, or ``{{brief:...}}`` (checked verbatim against
  docs/BUILD_BRIEF.md). An allowance renders as its text.
- ``python3 tools/claims.py render`` writes each document from its template
  and the current artifacts. tools/claimcheck.py proves every number in a
  document is a template literal that passed the allowance rules or a claim
  value rendered from a committed version of its source. tools/freshness.py
  proves every claim value equals what the current source gives.

Commands of this module: ``render [DOC ...]`` and ``show``. Stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import string
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import prereg  # noqa: E402
from chainlib import (  # noqa: E402
    GitError,
    blob_bytes,
    blob_id,
    commits_touching,
    git,
    sha256_file,
    tracked_files,
)

REGISTRY = "docs/claims.json"
BRIEF = "docs/BUILD_BRIEF.md"
BRIEF_SHA256 = "docs/BUILD_BRIEF.sha256"
SCHEMA = "baserate-claims/1"
CLAIM_KEYS = frozenset({"source", "select", "format", "note"})
ALLOWANCE_KINDS: dict[str, str] = {
    "param": "design parameter (prevalence, budget, sample size, grid value, nominal level)",
    "threshold": "frozen threshold from a preregistration or the brief",
    "cap": "usage cap from budget.toml or the brief",
    "date": "calendar date or year (YYYY, YYYY-MM, or YYYY-MM-DD)",
    "version": "software or document version",
    "label": "section, gate, RQ, experiment, hypothesis, table, or figure label",
    "name": "identifier that contains digits (model, package, or file name)",
    "brief": "text quoted verbatim from docs/BUILD_BRIEF.md",
}
KINDS = frozenset({"claim", *ALLOWANCE_KINDS})
FREE_TEXT_MAX = 40
NAME_MAX = 64
BRIEF_QUOTE_MAX = 200
CLAIM_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")
PLACEHOLDER_START_RE = re.compile(r"\{\{[ \t]*([a-z]+)[ \t]*:")
PLACEHOLDER_RE = re.compile(r"\{\{[ \t]*([a-z]+)[ \t]*:([^{}\n]*)\}\}")
FILL = "\x00"

# ------------------------------------------------------------ number scanning
#
# A number is a run of digits that does not continue a word or a dotted run
# (so G5, RQ1, E2, H1, v1.0, sha256, and qwen2.5 are identifiers, not numbers),
# with optional thousands separators, decimals, and exponent: 7, 0.412, 10,000,
# 1e-3. The exclusions below blank spans that are addresses or identifiers, not
# claims; each one is covered by a test in tests/test_claimcheck.py.

NUM_RE = re.compile(r"(?<![\w.])(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'`)\]\x00]+")
LINK_DEST_RE = re.compile(r"\]\(([^)\s\x00]+)")
# Where a mark is drawn and where a link points are not claims (raise-v1
# GEOM_RE, plus href and src). The text a chart displays still is.
ATTR_RE = re.compile(
    r"\b(?:href|src|x|y|x1|x2|y1|y2|cx|cy|r|rx|ry|dx|dy|width|height|points|viewBox|offset"
    r"|style)=\"[^\"\n]*\""
)
HEX_RE = re.compile(r"(?<![0-9A-Za-z_])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,64}(?![\w])")
FOOTNOTE_RE = re.compile(r"\[\^[^\]\s]+\]")
LIST_RE = re.compile(r"^([ \t]*(?:>[ \t]*)*)(\d{1,9})([.)])(?=[ \t])", re.M)
HEADING_RE = re.compile(r"^(#{1,6}[ \t]+)((?:\d+\.)+)(?=[ \t])", re.M)
REF_RE = re.compile(
    r"(?:\b(?:[Ss]ections?|[Ss]teps?|[Tt]ables?|[Ff]igures?|[Ff]ig\.|[Aa]ppendix|[Rr]ungs?"
    r"|[Nn]on-negotiables?|[Mm]ilestones?)[ \t]+|§[ \t]?)(\d+(?:\.\d+)*)"
)
PATH_RE = re.compile(r"(?<![\w./\-\x00])(?:\./)?([A-Za-z0-9_][A-Za-z0-9_.\-]*)/[A-Za-z0-9_.\-/]*")
VERSION_RE = re.compile(r"v?\d+(?:\.\d+)+(?:[-+.]?[0-9A-Za-z]+)*|v\d+")


class ClaimError(ValueError):
    """A claim, template, or registry problem, with a message for authors."""


def _blank_spans(text: str, spans: Iterable[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            if chars[i] != "\n":
                chars[i] = FILL
    return "".join(chars)


def _blank(
    text: str,
    pattern: re.Pattern[str],
    group: int = 0,
    keep: Callable[[re.Match[str]], bool] | None = None,
) -> str:
    spans = [m.span(group) for m in pattern.finditer(text) if keep is None or keep(m)]
    return _blank_spans(text, spans) if spans else text


def find_numbers(text: str, top_dirs: frozenset[str] = frozenset()) -> list[tuple[int, str]]:
    """Numbers in ``text`` outside the principled exclusions.

    Args:
        text: Literal text (placeholders already blanked).
        top_dirs: Top-level directories of the repository; a path that starts
            with one (``prereg/0001-e0.md``) is an identifier.

    Returns:
        (offset, token) for every number found.
    """
    t = _blank(text, URL_RE)
    t = _blank(t, LINK_DEST_RE, 1)
    t = _blank(t, ATTR_RE)
    t = _blank(t, HEX_RE)
    t = _blank(t, FOOTNOTE_RE)
    t = _blank(t, LIST_RE, 2)
    t = _blank(t, HEADING_RE, 2)
    t = _blank(t, REF_RE, 1)
    t = _blank(t, PATH_RE, 0, keep=lambda m: m.group(1) in top_dirs)
    return [(m.start(), m.group(0)) for m in NUM_RE.finditer(t)]


# ------------------------------------------------------------------ selectors

FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]*")
COND_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_\-]*)=([^,\]|]+)")


@dataclass(frozen=True)
class Key:
    """Select an object member."""

    name: str


@dataclass(frozen=True)
class Index:
    """Select a list element (negative counts from the end)."""

    index: int


@dataclass(frozen=True)
class Where:
    """Select the one list element whose fields equal the given values."""

    conds: tuple[tuple[str, str], ...]


Step = Key | Index | Where


@dataclass(frozen=True)
class Op:
    """A final operator: ``len``, ``count:<conds>``, or ``head:<n>``."""

    name: str
    conds: tuple[tuple[str, str], ...] = ()
    n: int = 0


@dataclass(frozen=True)
class Selector:
    """A parsed ``select`` expression."""

    text: str
    steps: tuple[Step, ...]
    op: Op | None


def _parse_conds(text: str, expr: str) -> tuple[tuple[str, str], ...]:
    conds = []
    for part in text.split(","):
        m = COND_RE.fullmatch(part)
        if not m:
            raise ClaimError(f"select {expr!r}: bad filter {part!r} (want field=value)")
        conds.append((m.group(1), m.group(2)))
    return tuple(conds)


def parse_selector(text: str) -> Selector:
    """Parse a ``select`` expression.

    Grammar (small on purpose, so it can be audited by reading it)::

        select := step ('.' step)* ('|' op)?      an empty select is the whole file
        step   := key? ('[' (int | "json-string" | field=value,...) ']')*
        op     := len | count:field=value,... | head:N

    ``a.b`` reads members, ``[2]`` and ``[-1]`` index lists, ``["0.001"]``
    reads a member whose name is not a plain key, and ``[metric=recall,budget=20]``
    selects the one list element whose fields equal those values (numbers
    compare as numbers). A filter that matches zero or several elements is an
    error, never a guess.

    Raises:
        ClaimError: If the expression does not parse.
    """
    expr, bar, op_text = text.partition("|")
    steps: list[Step] = []
    i, n = 0, len(expr)
    after_dot = False
    while i < n:
        c = expr[i]
        if c == ".":
            if not steps or after_dot:
                raise ClaimError(f"select {text!r}: misplaced '.' at offset {i}")
            after_dot, i = True, i + 1
            continue
        if c == "[":
            if after_dot:
                raise ClaimError(f"select {text!r}: '.' must be followed by a key")
            if expr.startswith('["', i):
                try:
                    key, end = json.JSONDecoder().raw_decode(expr, i + 1)
                except ValueError as exc:
                    raise ClaimError(f"select {text!r}: bad quoted key: {exc}") from exc
                if not expr.startswith("]", end):
                    raise ClaimError(f"select {text!r}: quoted key must be followed by ']'")
                steps.append(Key(str(key)))
                i = end + 1
                continue
            close = expr.find("]", i)
            if close < 0:
                raise ClaimError(f"select {text!r}: unclosed '['")
            inner = expr[i + 1 : close]
            if re.fullmatch(r"-?[0-9]+", inner):
                steps.append(Index(int(inner)))
            else:
                steps.append(Where(_parse_conds(inner, text)))
            i = close + 1
            continue
        m = FIELD_RE.match(expr, i)
        if m is None or (steps and not after_dot):
            raise ClaimError(f"select {text!r}: unexpected {c!r} at offset {i}")
        steps.append(Key(m.group(0)))
        after_dot, i = False, m.end()
    if after_dot:
        raise ClaimError(f"select {text!r}: ends with '.'")
    op: Op | None = None
    if bar:
        if op_text == "len":
            op = Op("len")
        elif op_text.startswith("count:"):
            op = Op("count", conds=_parse_conds(op_text[len("count:") :], text))
        elif re.fullmatch(r"head:[1-9][0-9]*", op_text):
            op = Op("head", n=int(op_text[len("head:") :]))
        else:
            raise ClaimError(f"select {text!r}: unknown operator {op_text!r}")
    return Selector(text, tuple(steps), op)


def _cond_holds(item: Mapping[str, Any], key: str, want: str) -> bool:
    if key not in item:
        return False
    value = item[key]
    if isinstance(value, bool):
        return want == ("true" if value else "false")
    if value is None:
        return want == "null"
    if isinstance(value, int | float):
        try:
            return float(want) == float(value)
        except ValueError:
            return False
    return isinstance(value, str) and value == want


def _where(items: Any, conds: tuple[tuple[str, str], ...], at: str) -> list[Any]:
    if not isinstance(items, list):
        raise ClaimError(f"filter at {at} applies to a {type(items).__name__}, not a list")
    return [x for x in items if isinstance(x, dict) and all(_cond_holds(x, k, v) for k, v in conds)]


def _conds_text(conds: tuple[tuple[str, str], ...]) -> str:
    return ",".join(f"{k}={v}" for k, v in conds)


def select(doc: Any, selector: Selector) -> Any:
    """Apply a selector to a parsed source.

    Raises:
        ClaimError: If any step does not resolve to exactly one value.
    """
    cur = doc
    at = "top level"
    for step in selector.steps:
        if isinstance(step, Key):
            if not isinstance(cur, dict) or step.name not in cur:
                raise ClaimError(f"no key {step.name!r} at {at}")
            cur = cur[step.name]
            at = f"{at}.{step.name}" if at != "top level" else step.name
        elif isinstance(step, Index):
            if not isinstance(cur, list) or not -len(cur) <= step.index < len(cur):
                raise ClaimError(f"no element [{step.index}] at {at}")
            cur = cur[step.index]
            at = f"{at}[{step.index}]"
        else:
            hits = _where(cur, step.conds, at)
            if len(hits) != 1:
                raise ClaimError(
                    f"filter [{_conds_text(step.conds)}] at {at} matches {len(hits)} elements, "
                    "needs exactly 1"
                )
            cur = hits[0]
            at = f"{at}[{_conds_text(step.conds)}]"
    op = selector.op
    if op is None:
        return cur
    if op.name == "len":
        if not isinstance(cur, list | dict | str):
            raise ClaimError(f"len applies to a list, object, or string, not {type(cur).__name__}")
        return len(cur)
    if op.name == "count":
        return len(_where(cur, op.conds, at))
    if not isinstance(cur, str):
        raise ClaimError(f"head applies to a string, not {type(cur).__name__}")
    return cur[: op.n]


# -------------------------------------------------------------------- formats


@dataclass(frozen=True)
class Field:
    """One piece of a format: literal text, then an optional replacement field."""

    literal: str
    name: str | None
    spec: str = ""


def parse_format(fmt: str) -> tuple[Field, ...]:
    """Parse and check a claim's ``format`` (Python format-string syntax).

    ``{}`` is the selected value and ``{key}`` a member of a selected object;
    each takes a format spec such as ``{:.3f}``, ``{:,d}``, or ``{:.1%}``.
    Literal text in a format may not contain digits: fixed numbers belong in
    the template, where they need an allowance.

    Raises:
        ClaimError: If the format is not allowed.
    """
    try:
        parsed = list(string.Formatter().parse(fmt))
    except ValueError as exc:
        raise ClaimError(f"format {fmt!r}: {exc}") from exc
    fields: list[Field] = []
    for literal, name, spec, conversion in parsed:
        if re.search(r"\d", literal):
            raise ClaimError(
                f"format {fmt!r} has digits outside its fields; write fixed numbers in the "
                "template as allowances"
            )
        if name is None:
            fields.append(Field(literal, None))
            continue
        if conversion is not None:
            raise ClaimError(f"format {fmt!r}: conversions such as !r are not allowed")
        if name and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ClaimError(f"format {fmt!r}: field {{{name}}} must be {{}} or a plain key")
        if spec and "{" in spec:
            raise ClaimError(f"format {fmt!r}: nested fields are not allowed")
        fields.append(Field(literal, name, spec or ""))
    if not any(f.name is not None for f in fields):
        raise ClaimError(f"format {fmt!r} has no {{}} field")
    return tuple(fields)


def _leaf(value: Any, what: str) -> int | float | str:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ClaimError(f"{what} is a {type(value).__name__}; a claim renders numbers or strings")
    leaf: int | float | str = value
    if isinstance(leaf, float) and not math.isfinite(leaf):
        raise ClaimError(f"{what} is {leaf}")
    return leaf


def render_value(value: Any, fields: Sequence[Field]) -> str:
    """Render a selected value with a parsed format.

    Raises:
        ClaimError: If a field is missing, has the wrong type, or the result
            is empty, spans lines, or contains placeholder braces.
    """
    out: list[str] = []
    for f in fields:
        out.append(f.literal)
        if f.name is None:
            continue
        if f.name == "":
            leaf = _leaf(value, "the selected value")
        else:
            if not isinstance(value, dict) or f.name not in value:
                raise ClaimError(f"the selected value has no key {f.name!r}")
            leaf = _leaf(value[f.name], f"key {f.name!r}")
        try:
            out.append(format(leaf, f.spec))
        except (TypeError, ValueError) as exc:
            raise ClaimError(f"cannot format {leaf!r} with {f.spec!r}: {exc}") from exc
    text = "".join(out)
    if not text or "\n" in text or "{{" in text or "}}" in text:
        raise ClaimError(f"rendering {text!r} must be one non-empty line without '{{{{' or '}}}}'")
    return text


# ------------------------------------------------------------ registry, claims


@dataclass(frozen=True)
class Claim:
    """A named claim: a source file and a deterministic extractor."""

    id: str
    source: str
    selector: Selector
    fields: tuple[Field, ...]
    format: str

    def render(self, doc: Any) -> str:
        """Render this claim from a parsed source."""
        return render_value(select(doc, self.selector), self.fields)


@dataclass
class Registry:
    """The parsed claims registry (docs/claims.json)."""

    path: str
    documents: dict[str, str]
    claims: dict[str, Claim]
    problems: list[str] = field(default_factory=list)
    invalid: set[str] = field(default_factory=set)


def _safe_rel(path: str) -> bool:
    p = PurePosixPath(path)
    return bool(path) and not p.is_absolute() and ".." not in p.parts and "\\" not in path


def check_source_path(source: str) -> None:
    """A claim's source is a JSON artifact under artifacts/ or a preregistration.

    Raises:
        ClaimError: If it is anything else.
    """
    if not _safe_rel(source):
        raise ClaimError(f"source {source!r} must be a plain repository-relative path")
    if source.startswith("artifacts/") and source.endswith(".json"):
        return
    if prereg.NAME_RE.match(source):
        return
    raise ClaimError(
        f"source {source!r} must be a JSON file under artifacts/ or a prereg/NNNN-name.md"
    )


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def parse_registry(text: str, path: str = REGISTRY) -> Registry:
    """Parse the registry text; entry problems are collected, not raised.

    Raises:
        ClaimError: If the file is not JSON or lacks the required structure.
    """
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicates)
    except ValueError as exc:
        raise ClaimError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ClaimError(f'{path} must be a JSON object with "schema": "{SCHEMA}"')
    documents = data.get("documents")
    claims_raw = data.get("claims")
    if not isinstance(documents, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in documents.items()
    ):
        raise ClaimError(f'{path}: "documents" must map document paths to template paths')
    if not isinstance(claims_raw, dict):
        raise ClaimError(f'{path}: "claims" must map claim ids to claim objects')
    registry = Registry(path=path, documents=dict(documents), claims={})
    for doc, template in documents.items():
        if not (_safe_rel(doc) and _safe_rel(template)) or doc == template:
            registry.problems.append(
                f"{path}: document {doc!r} -> template {template!r} is invalid"
            )
    for cid, raw in claims_raw.items():
        try:
            registry.claims[cid] = _parse_claim(cid, raw)
        except ClaimError as exc:
            registry.invalid.add(cid)
            registry.problems.append(f"{path}: claim {cid!r}: {exc}")
    return registry


def _parse_claim(cid: str, raw: Any) -> Claim:
    if not CLAIM_ID_RE.fullmatch(cid):
        raise ClaimError("ids use letters, digits, '.', '_', and '-'")
    if not isinstance(raw, dict):
        raise ClaimError("must be an object")
    unknown = sorted(set(raw) - CLAIM_KEYS)
    if unknown:
        raise ClaimError(f"unknown keys {unknown}; allowed: {sorted(CLAIM_KEYS)}")
    source, sel, fmt = raw.get("source"), raw.get("select", ""), raw.get("format", "{}")
    if not isinstance(source, str) or not isinstance(sel, str) or not isinstance(fmt, str):
        raise ClaimError("source, select, and format must be strings")
    check_source_path(source)
    return Claim(cid, source, parse_selector(sel), parse_format(fmt), fmt)


def parse_source(source: str, data: bytes) -> Any:
    """Parse a claim source: artifact JSON, or a preregistration's config block.

    Raises:
        ClaimError: If it does not parse.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ClaimError(f"{source} is not UTF-8") from exc
    if source.startswith("prereg/"):
        try:
            return prereg.parse_config(text)
        except prereg.PreregError as exc:
            raise ClaimError(f"{source}: {exc}") from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ClaimError(f"{source} is not valid JSON: {exc}") from exc


# ------------------------------------------------------------------ templates


@dataclass(frozen=True)
class Placeholder:
    """One ``{{kind:text}}`` tag in a template."""

    kind: str
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class Piece:
    """Part of a template after allowances are expanded: literal text or a claim.

    ``origins`` maps the piece back to the template: (start in ``text``,
    template offset, whether the text is copied verbatim from the template).
    """

    text: str
    offset: int
    claim: bool = False
    origins: tuple[tuple[int, int, bool], ...] = ()

    def template_offset(self, index: int) -> int:
        """Template offset of character ``index`` of this piece."""
        best = self.offset
        for start, at, verbatim in self.origins:
            if start > index:
                break
            best = at + (index - start) if verbatim else at
        return best


@dataclass
class Template:
    """A parsed template."""

    path: str
    text: str
    placeholders: list[Placeholder]
    problems: list[tuple[int, str]] = field(default_factory=list)

    def line_of(self, offset: int) -> int:
        """1-based template line of an offset."""
        return self.text.count("\n", 0, offset) + 1

    def line_text(self, offset: int) -> str:
        """The template line holding an offset, trimmed for messages."""
        start = self.text.rfind("\n", 0, offset) + 1
        end = self.text.find("\n", offset)
        return self.text[start : end if end >= 0 else None].strip()[:120]

    def claims(self) -> list[Placeholder]:
        """The claim placeholders, in order."""
        return [p for p in self.placeholders if p.kind == "claim"]

    def literal_view(self) -> str:
        """The template text with every placeholder blanked, offsets preserved."""
        return _blank_spans(self.text, ((p.start, p.end) for p in self.placeholders))

    def pieces(self) -> list[Piece]:
        """Literal pieces (allowances expanded and merged) and claim pieces."""
        out: list[Piece] = []
        parts: list[tuple[str, int, bool]] = []

        def flush() -> None:
            text = "".join(t for t, _, _ in parts)
            if text:
                origins, n = [], 0
                for t, at, verbatim in parts:
                    origins.append((n, at, verbatim))
                    n += len(t)
                out.append(Piece(text, parts[0][1], origins=tuple(origins)))
            parts.clear()

        pos = 0
        for p in self.placeholders:
            parts.append((self.text[pos : p.start], pos, True))
            if p.kind == "claim":
                flush()
                out.append(Piece(p.text, p.start, claim=True))
            else:
                parts.append((p.text, p.start, False))
            pos = p.end
        parts.append((self.text[pos:], pos, True))
        flush()
        return out

    def render(self, values: Mapping[str, str]) -> str:
        """The document this template renders to, given each claim's value."""
        out: list[str] = []
        pos = 0
        for p in self.placeholders:
            out.append(self.text[pos : p.start])
            out.append(values[p.text] if p.kind == "claim" else p.text)
            pos = p.end
        out.append(self.text[pos:])
        return "".join(out)


def parse_template(path: str, text: str) -> Template:
    """Find the placeholders of a template and any syntax problems."""
    template = Template(path=path, text=text, placeholders=[])
    for m in PLACEHOLDER_START_RE.finditer(text):
        kind = m.group(1)
        line = text.count("\n", 0, m.start()) + 1
        if kind not in KINDS:
            template.problems.append((line, f"unknown placeholder kind {kind!r}"))
            continue
        full = PLACEHOLDER_RE.match(text, m.start())
        if full is None:
            template.problems.append(
                (line, f"malformed {{{{{kind}:...}}}} (one line, no braces inside)")
            )
            continue
        body = full.group(2).strip()
        if kind == "claim" and not CLAIM_ID_RE.fullmatch(body):
            template.problems.append((line, f"bad claim id {body!r}"))
            continue
        if not body:
            template.problems.append((line, f"empty {{{{{kind}:}}}}"))
            continue
        template.placeholders.append(Placeholder(kind, body, full.start(), full.end()))
    return template


def allowance_problem(kind: str, text: str, brief: Callable[[], str]) -> str | None:
    """Why an allowance tag is not acceptable, or None if it is.

    Args:
        kind: The allowance kind.
        text: The tag's text.
        brief: Returns docs/BUILD_BRIEF.md with whitespace collapsed (raises
            ClaimError when the brief is unavailable).
    """
    if not re.search(r"\d", text):
        return "holds no number, so it needs no allowance"
    if kind == "date":
        return None if _is_date(text) else "is not YYYY, YYYY-MM, or YYYY-MM-DD"
    if kind == "version":
        return None if VERSION_RE.fullmatch(text) else "is not a version such as 3.11 or v1.0"
    if kind == "name":
        if len(text) > NAME_MAX or re.search(r"\s", text) or not re.search(r"[A-Za-z]", text):
            return f"must be one token of at most {NAME_MAX} characters with a letter"
        return None
    if kind == "brief":
        if len(text) > BRIEF_QUOTE_MAX:
            return f"is longer than {BRIEF_QUOTE_MAX} characters"
        try:
            whole = brief()
        except ClaimError as exc:
            return str(exc)
        if " ".join(text.split()) not in whole:
            return f"is not quoted verbatim from {BRIEF}"
        return None
    if len(text) > FREE_TEXT_MAX:
        return f"is longer than {FREE_TEXT_MAX} characters; split it or make it a claim"
    return None


def _is_date(text: str) -> bool:
    m = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", text)
    if not m or not 1900 <= int(m.group(1)) <= 2100:
        return False
    if m.group(3):
        try:
            dt.date.fromisoformat(text)
        except ValueError:
            return False
    elif m.group(2):
        return 1 <= int(m.group(2)) <= 12
    return True


# --------------------------------------------------------------------- matching


@dataclass(frozen=True)
class Mismatch:
    """Where a document stops matching its template.

    ``piece`` is the first piece that could not be matched (``len(pieces)``
    means text after the template's end) and ``positions`` the document
    positions it was tried from.
    """

    piece: int
    positions: tuple[int, ...]


Ends = Callable[[Piece, int], Iterable[int]]


def decompose(doc: str, pieces: Sequence[Piece], ends: Ends) -> list[tuple[int, int]] | Mismatch:
    """Split ``doc`` into one span per piece, or say where that is impossible.

    ``ends(piece, pos)`` gives every end position the piece may match from
    ``pos``. This is a dynamic program over document positions, so a claim
    value that happens to contain the following literal text cannot derail it.

    Returns:
        (start, end) per piece, or a Mismatch at the furthest point reached.
    """
    frontier: dict[int, int] = {0: -1}
    back: list[dict[int, int]] = []
    for k, piece in enumerate(pieces):
        nxt: dict[int, int] = {}
        for pos in sorted(frontier):
            for end in ends(piece, pos):
                nxt.setdefault(end, pos)
        if not nxt:
            return Mismatch(k, tuple(sorted(frontier)))
        back.append(nxt)
        frontier = nxt
    if len(doc) not in frontier:
        return Mismatch(len(pieces), tuple(sorted(frontier)))
    spans: list[tuple[int, int]] = []
    pos = len(doc)
    for k in range(len(pieces) - 1, -1, -1):
        start = back[k][pos]
        spans.append((start, pos))
        pos = start
    spans.reverse()
    return spans


def literal_ends(doc: str, piece: Piece, pos: int) -> list[int]:
    """End position of a literal piece at ``pos``, if it matches there."""
    return [pos + len(piece.text)] if doc.startswith(piece.text, pos) else []


def options_ends(doc: str, options: Mapping[str, Sequence[str]]) -> Ends:
    """Claims match one of their allowed renderings exactly."""

    def ends(piece: Piece, pos: int) -> list[int]:
        if not piece.claim:
            return literal_ends(doc, piece, pos)
        return [pos + len(o) for o in options.get(piece.text, ()) if doc.startswith(o, pos)]

    return ends


def wildcard_ends(doc: str) -> Ends:
    """Claims match any non-empty text on one line (for diagnostics only)."""

    def ends(piece: Piece, pos: int) -> Iterable[int]:
        if not piece.claim:
            return literal_ends(doc, piece, pos)
        eol = doc.find("\n", pos)
        return range(pos + 1, (len(doc) if eol < 0 else eol) + 1)

    return ends


def line_col(text: str, offset: int) -> tuple[int, int]:
    """1-based line and column of an offset."""
    line = text.count("\n", 0, offset) + 1
    return line, offset - (text.rfind("\n", 0, offset) + 1) + 1


def describe_mismatch(
    doc_rel: str, doc: str, template: Template, pieces: Sequence[Piece], miss: Mismatch
) -> str:
    """A message saying where a document departs from its template."""
    if miss.piece >= len(pieces):
        pos = max(miss.positions)
        line, col = line_col(doc, pos)
        return (
            f"{doc_rel}:{line}:{col}: text after the end of its template "
            f"{template.path}: {doc[pos : pos + 40]!r}"
        )
    piece = pieces[miss.piece]
    if piece.claim:
        line, col = line_col(doc, max(miss.positions))
        return (
            f"{doc_rel}:{line}:{col}: no value where {template.path}:"
            f"{template.line_of(piece.offset)} has claim {piece.text}"
        )

    def agreed(pos: int) -> int:
        n = 0
        while n < len(piece.text) and pos + n < len(doc) and doc[pos + n] == piece.text[n]:
            n += 1
        return n

    pos = max(miss.positions, key=lambda p: (agreed(p), -p))
    common = agreed(pos)
    line, col = line_col(doc, pos + common)
    tline = template.line_of(piece.template_offset(common))
    return (
        f"{doc_rel}:{line}:{col}: does not match its template {template.path}:{tline}: expected "
        f"{piece.text[common : common + 40]!r}, found {doc[pos + common : pos + common + 40]!r} "
        "(edit the template, not the document, then run python3 tools/claims.py render)"
    )


# -------------------------------------------------------------------- workspace


def resolve_root(path: Path) -> Path:
    """The work-tree top level containing ``path``, or ``path`` itself outside git."""
    try:
        return Path(git(path, "rev-parse", "--show-toplevel"))
    except GitError:
        return path.resolve()


def doc_rel(root: Path, arg: str) -> str:
    """A document argument as a repository-relative POSIX path."""
    p = Path(arg)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(root.resolve())
        except ValueError:
            return arg
    return PurePosixPath(p.as_posix()).as_posix()


class Workspace:
    """Repository state shared by claimcheck, freshness, and render (with caches)."""

    def __init__(self, root: Path, registry_path: str = REGISTRY) -> None:
        """Bind to a repository root and a registry path relative to it."""
        self.root = root
        self.registry_path = registry_path
        self._registry: Registry | None = None
        self._tracked: frozenset[str] | None = None
        self._brief: str | None = None
        self._versions: dict[str, list[Any]] = {}
        self._current: dict[str, Any] = {}

    def registry(self) -> Registry:
        """The parsed registry.

        Raises:
            ClaimError: If it is missing or malformed.
        """
        if self._registry is None:
            path = self.root / self.registry_path
            if not path.is_file():
                raise ClaimError(
                    f"{self.registry_path} not found: results documents are rendered from "
                    "templates whose claims it registers (docs/CLAIMS.md)"
                )
            self._registry = parse_registry(path.read_text(encoding="utf-8"), self.registry_path)
        return self._registry

    def template_for(self, doc: str) -> Template:
        """The parsed template of a registered document.

        Raises:
            ClaimError: If the document is not registered or its template is missing.
        """
        registry = self.registry()
        rel = registry.documents.get(doc)
        if rel is None:
            raise ClaimError(
                f'no template registered for it in {registry.path} "documents"; results '
                "documents are rendered from templates (docs/CLAIMS.md)"
            )
        path = self.root / rel
        if not path.is_file():
            raise ClaimError(f"its template {rel} is missing")
        return parse_template(rel, path.read_text(encoding="utf-8"))

    def tracked(self) -> frozenset[str]:
        """Tracked paths (git index)."""
        if self._tracked is None:
            self._tracked = frozenset(tracked_files(self.root))
        return self._tracked

    def top_dirs(self) -> frozenset[str]:
        """Top-level directories that hold tracked files."""
        return frozenset(p.split("/", 1)[0] for p in self.tracked() if "/" in p)

    def brief(self) -> str:
        """docs/BUILD_BRIEF.md with whitespace collapsed, checked against its hash.

        Raises:
            ClaimError: If the brief is missing or does not match docs/BUILD_BRIEF.sha256.
        """
        if self._brief is None:
            path = self.root / BRIEF
            if not path.is_file():
                raise ClaimError(f"cannot check a brief quote: {BRIEF} is missing")
            pinned = self.root / BRIEF_SHA256
            if pinned.is_file():
                want = pinned.read_text(encoding="utf-8").split()[:1]
                if want != [sha256_file(path)]:
                    raise ClaimError(f"{BRIEF} does not match {BRIEF_SHA256}")
            self._brief = " ".join(path.read_text(encoding="utf-8").split())
        return self._brief

    def current(self, claim: Claim) -> str:
        """The claim rendered from its source as it is now in the work tree.

        Raises:
            ClaimError: If the source is missing or the claim does not resolve.
        """
        if claim.source not in self._current:
            path = self.root / claim.source
            if not path.is_file():
                raise ClaimError(f"source {claim.source} is missing")
            self._current[claim.source] = parse_source(claim.source, path.read_bytes())
        try:
            return claim.render(self._current[claim.source])
        except ClaimError as exc:
            raise ClaimError(f"cannot re-derive from {claim.source}: {exc}") from exc

    def _source_versions(self, source: str) -> list[Any]:
        """Every committed version of a source, parsed (unparsable ones skipped)."""
        if source not in self._versions:
            oids: list[str] = []
            for commit in commits_touching(self.root, source, merges=True):
                oid = blob_id(self.root, commit, source)
                if oid is not None and oid not in oids:
                    oids.append(oid)
            parsed: list[Any] = []
            for oid in oids:
                try:
                    parsed.append(parse_source(source, blob_bytes(self.root, oid)))
                except ClaimError:
                    continue
            if oids and not parsed:
                raise ClaimError(f"no committed version of {source} parses")
            self._versions[source] = parsed
        return self._versions[source]

    def banked(self, claim: Claim) -> list[str]:
        """Every rendering of the claim from a committed version of its source.

        Raises:
            ClaimError: If the source is untracked, uncommitted, or the claim
                resolves in no committed version.
        """
        if claim.source not in self.tracked():
            raise ClaimError(f"source {claim.source} is not tracked by git, so it is not banked")
        versions = self._source_versions(claim.source)
        if not versions:
            raise ClaimError(f"source {claim.source} has never been committed, so it is not banked")
        out: list[str] = []
        first_error = ""
        for doc in versions:
            try:
                text = claim.render(doc)
            except ClaimError as exc:
                first_error = first_error or str(exc)
                continue
            if text not in out:
                out.append(text)
        if not out:
            raise ClaimError(f"resolves in no committed version of {claim.source}: {first_error}")
        return out


# -------------------------------------------------------------------------- CLI


def cmd_render(ws: Workspace, docs: Sequence[str]) -> int:
    """Write each document from its template and the current sources."""
    try:
        registry = ws.registry()
    except ClaimError as exc:
        print(f"render: {exc}")
        return 1
    failed = 0
    for doc in docs or sorted(registry.documents):
        try:
            template = ws.template_for(doc)
        except ClaimError as exc:
            print(f"render: {doc}: {exc}")
            failed += 1
            continue
        problems = [f"{template.path}:{line}: {msg}" for line, msg in template.problems]
        values: dict[str, str] = {}
        for p in template.claims():
            claim = registry.claims.get(p.text)
            try:
                if claim is None:
                    raise ClaimError(f"claim {p.text!r} is not registered (or invalid)")
                values[p.text] = ws.current(claim)
            except ClaimError as exc:
                problems.append(f"{template.path}:{template.line_of(p.start)}: {p.text}: {exc}")
        if problems:
            for problem in problems:
                print(f"render: {problem}")
            failed += 1
            continue
        text = template.render(values)
        target = ws.root / doc
        old = target.read_text(encoding="utf-8") if target.is_file() else None
        if old == text:
            print(f"unchanged {doc} ({len(template.claims())} claims)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"rendered  {doc} from {template.path} ({len(template.claims())} claims)")
    return 1 if failed else 0


def cmd_show(ws: Workspace) -> int:
    """Print every registered claim with its current value."""
    try:
        registry = ws.registry()
    except ClaimError as exc:
        print(f"show: {exc}")
        return 1
    for problem in registry.problems:
        print(f"invalid  {problem}")
    bad = len(registry.problems)
    for cid, claim in sorted(registry.claims.items()):
        try:
            print(f"{cid}\t{ws.current(claim)}\t{claim.source}")
        except ClaimError as exc:
            print(f"{cid}\tERROR {exc}\t{claim.source}")
            bad += 1
    return 1 if bad else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point: ``render`` and ``show``."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    parser.add_argument("--registry", default=REGISTRY, help="registry path (repo-relative)")
    sub = parser.add_subparsers(dest="command", required=True)
    p_render = sub.add_parser("render", help="write documents from their templates")
    p_render.add_argument("docs", nargs="*", help="documents (default: all registered)")
    sub.add_parser("show", help="print every claim with its current value")
    args = parser.parse_args(argv)
    root = resolve_root(args.root)
    ws = Workspace(root, args.registry)
    if args.command == "show":
        return cmd_show(ws)
    return cmd_render(ws, [doc_rel(root, d) for d in args.docs])


if __name__ == "__main__":
    sys.exit(main())
