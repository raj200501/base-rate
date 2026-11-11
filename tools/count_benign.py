"""Count benign real-agent runs under the fixed oracle (prereg/0006), for the E1 stopping rule.

    python tools/count_benign.py              # total benign real runs
    python tools/count_benign.py --split 700  # insertion attempts per model: "<1.7B> <4B>"

Every run in runs/agents/corpus-*/traces.jsonl is relabeled by exact replay
(baserate.traces.runner.relabel) before counting. The split gives the 4B model
its proportional share of attempts rounded to the nearest integer (halves up)
and the rest to the first listed model (prereg/0002-e1-corpus.md, read as in
DECISIONS.md D23).
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import Counter

from baserate.traces.domains import get_domain
from baserate.traces.runner import relabel
from baserate.traces.schema import Trace

FIRST, SECOND = "qwen3-1.7b-q8_0", "qwen3-4b-q4_k_m"


def counts() -> Counter[str]:
    """Benign real-agent runs per subject model."""
    out: Counter[str] = Counter()
    for path in glob.glob("runs/agents/corpus-*/traces.jsonl"):
        with open(path, encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.strip()]
        for line in lines:
            t = Trace.from_dict(json.loads(line))
            r = relabel(get_domain(t.domain), t)
            if r is not None and r.source == "real_agent" and r.label == "benign":
                out[r.agent_id] += 1
    return out


def split_attempts(attempts: int, first: int, second: int) -> tuple[int, int]:
    """Attempts per model: the second's share rounded half up, the rest to the first (D23)."""
    total = first + second
    share = (2 * attempts * second + total) // (2 * total) if total else 0
    return attempts - share, share


def main(argv: list[str] | None = None) -> int:
    """Print the total, or the per-model split of insertion attempts."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--split", type=int, default=0)
    args = ap.parse_args(argv)
    c = counts()
    if args.split:
        print(*split_attempts(args.split, c[FIRST], c[SECOND]))
    else:
        print(sum(c.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
