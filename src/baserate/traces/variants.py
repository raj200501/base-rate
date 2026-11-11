"""Procedural variants of traces (always labeled synthetic; same source run).

A variant re-renders one trace's monitor-visible text with consistent,
meaning-preserving changes, so the corpus reaches its benign target without
pretending the copies are independent:

- entity ids such as ``T-1234`` or ``INC-56`` are renamed consistently
  throughout the trace (same prefix and digit count, no collisions);
- standalone amounts and counts are remapped consistently (a value maps to
  the same new value everywhere it appears; dates, versions, and ids are left
  alone);
- JSON observations get a different key order and spacing;
- a few common phrases in free text are swapped for synonyms.

Labels, the violation type, and ``first_violating_step`` are unchanged. The
variant keeps the original's ``source_run_id``, so every interval clusters it
with its source run, and it is marked ``source="procedural"`` and
``synthetic=True``. The change list is recorded in provenance.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from baserate.traces.schema import Action, Trace

ID_RE = re.compile(r"\b([A-Z]{1,4})-(\d{2,6})\b")
NUM_RE = re.compile(r"(?<![\w.\-/:@])(\d{1,5})(?![\w\-/:@]|\.\d)")
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
SYNONYMS: tuple[tuple[str, str], ...] = (
    ("Thank you", "Thanks"),
    ("thank you", "thanks"),
    ("issue", "problem"),
    ("Hello", "Hi"),
    ("as requested", "as asked"),
    ("completed", "finished"),
    ("please", "kindly"),
    ("updated", "changed"),
)


def _id_map(texts: list[str], rng: random.Random) -> dict[str, str]:
    """Consistent new ids for every id in ``texts`` (same prefix and width)."""
    found = sorted({m.group(0) for t in texts for m in ID_RE.finditer(t)})
    taken = set(found)
    mapping: dict[str, str] = {}
    for old in found:
        prefix, digits = old.split("-", 1)
        width = len(digits)
        lo = 10 ** (width - 1) if width > 1 else 0
        for _ in range(1000):
            new = f"{prefix}-{rng.randint(lo, 10**width - 1):0{width}d}"
            if new not in taken:
                break
        taken.add(new)
        mapping[old] = new
    return mapping


def _num_map(texts: list[str], rng: random.Random) -> dict[str, str]:
    """Consistent new values for standalone integers outside ids and dates."""
    values: set[str] = set()
    for t in texts:
        masked = DATE_RE.sub(" ", ID_RE.sub(" ", t))
        values.update(m.group(1) for m in NUM_RE.finditer(masked))
    mapping: dict[str, str] = {}
    used = set(values)
    for v in sorted(values, key=int):
        n = int(v)
        if n < 2:
            continue  # keep 0 and 1 (often booleans, counts of one, or list indices)
        for _ in range(100):
            new = str(max(2, round(n * rng.uniform(0.6, 1.6))))
            if new not in used or new == v:
                break
        used.add(new)
        mapping[v] = new
    return mapping


def _apply(text: str, ids: dict[str, str], nums: dict[str, str], syn: list[tuple[str, str]]) -> str:
    """Rename ids, remap numbers (outside ids and dates), and swap synonyms."""
    out = ID_RE.sub(lambda m: ids.get(m.group(0), m.group(0)), text)
    protected = [(m.start(), m.end()) for m in ID_RE.finditer(out)]
    protected += [(m.start(), m.end()) for m in DATE_RE.finditer(out)]

    def num(m: re.Match[str]) -> str:
        if any(a <= m.start() < b for a, b in protected):
            return m.group(0)
        return nums.get(m.group(1), m.group(1))

    out = NUM_RE.sub(num, out)
    for a, b in syn:
        out = out.replace(a, b)
    return out


def _rejson(text: str, rng: random.Random) -> str:
    """Reorder keys and change spacing of a JSON object; other text unchanged."""
    try:
        obj = json.loads(text)
    except ValueError:
        return text
    if not isinstance(obj, dict):
        return text
    keys = list(obj)
    rng.shuffle(keys)
    seps = rng.choice([(", ", ": "), (",", ":"), (", ", ":")])
    return json.dumps({k: obj[k] for k in keys}, separators=seps)


def make_variant(trace: Trace, index: int) -> Trace:
    """The ``index``-th procedural variant of ``trace`` (deterministic).

    Args:
        trace: A trace whose actions follow the corpus layout.
        index: Variant number (1, 2, ...).

    Returns:
        A synthetic trace in the same source-run cluster with the same labels.
    """
    rng = random.Random(f"variant:{trace.trace_id}:{index}")
    texts = [trace.final_answer] + [f"{a.input}\n{a.observation}" for a in trace.actions]
    ids = _id_map(texts, rng)
    nums = _num_map(texts, rng)
    syn = [pair for pair in SYNONYMS if rng.random() < 0.5]
    rejson = rng.random() < 0.7

    def tx(text: str) -> str:
        return _apply(text, ids, nums, syn)

    def obs(text: str) -> str:
        changed = tx(text)
        return _rejson(changed, rng) if rejson else changed

    actions = tuple(
        Action(a.step, a.tool, _input(a, tx), obs(a.observation), dict(a.metadata))
        for a in trace.actions
    )
    prov: dict[str, Any] = {
        "variant_of": trace.trace_id,
        "variant_index": index,
        "transforms": {
            "ids_renamed": len(ids),
            "numbers_remapped": len(nums),
            "synonyms": [a for a, _ in syn],
            "json_reordered": rejson,
        },
        "source_provenance": {k: v for k, v in trace.provenance.items() if k != "raw"},
    }
    return replace(
        trace,
        trace_id=f"var:{trace.trace_id}:{index}",
        actions=actions,
        final_answer=tx(trace.final_answer),
        source="procedural",
        synthetic=True,
        provenance=prov,
    )


def _input(action: Action, tx: Callable[[str], str]) -> str:
    """Transform an action input, keeping JSON inputs valid JSON."""
    try:
        obj = json.loads(action.input)
    except ValueError:
        return tx(action.input)
    return json.dumps(_walk(obj, tx), sort_keys=True)


def _walk(obj: Any, tx: Callable[[str], str]) -> Any:
    if isinstance(obj, str):
        return tx(obj)
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, int):
        return int(tx(str(obj))) if tx(str(obj)).isdigit() else obj
    if isinstance(obj, list):
        return [_walk(x, tx) for x in obj]
    if isinstance(obj, dict):
        return {k: _walk(v, tx) for k, v in obj.items()}
    return obj


def variants(trace: Trace, count: int) -> list[Trace]:
    """``count`` variants of ``trace`` (indices 1..count)."""
    return [make_variant(trace, i) for i in range(1, count + 1)]
