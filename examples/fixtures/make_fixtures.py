"""Build the example score files from Armour's recorded traces.

The output is deterministic: the same inputs give byte-identical files. From
the repository root:

    uv run python examples/fixtures/make_fixtures.py          # write the files
    uv run python examples/fixtures/make_fixtures.py --check  # fail if out of date

Inputs are the verbatim Armour files under tests/fixtures/armour (Armour,
https://github.com/raj200501/armour, MIT License, commit 639eb62), checked
against the SHA-256 digests in their README before use. Every trace is a
recorded or scripted agent run on mock tools, and every violation is an
abstract mock action. The scores come from Armour's rule packs as ported in
baserate.monitors.rules. The files are demonstration data, not study results.

Outputs, in this directory unless --out is given:

- monitor_scores.csv: the reviewer-calibrated rule pack's score per trace.
- cascade_scores.csv: the baseline pack's score as stage 1 (score) and the
  reviewer-calibrated pack's score as stage 2 (stage2_score).

Clusters follow DECISIONS.md D12: Armour's dataset records come from a few
generator scripts, so a trace's cluster is its source file and agent family,
not its record.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from baserate.demo import group, verify_fixtures
from baserate.monitors.rules import ArmourRuleMonitor
from baserate.scores import ScoreSet, dumps_scores
from baserate.traces.armour import ARMOUR_COMMIT, load_armour_datasets

HERE = Path(__file__).resolve().parent
ARMOUR = HERE.parents[1] / "tests" / "fixtures" / "armour"
STAGE1_PACK = "baseline"
STAGE2_PACK = "reviewer-calibrated"
MONITOR_PACK = "reviewer-calibrated"

HEADER = (
    "Example scores for the base-rate examples. Demonstration data, not study results.",
    f"Traces: Armour labeled datasets (https://github.com/raj200501/armour, MIT License, "
    f"commit {ARMOUR_COMMIT[:7]}); recorded and scripted agent runs on mock tools.",
    "Clusters: source file and agent family of each record (DECISIONS.md D12).",
    "Built by examples/fixtures/make_fixtures.py; see examples/fixtures/README.md.",
)


def build() -> dict[str, str]:
    """The fixture files as {file name: text}."""
    verify_fixtures(ARMOUR)
    traces = list(load_armour_datasets(ARMOUR).traces)
    ids = tuple(t.trace_id for t in traces)
    labels = tuple(t.label for t in traces)
    clusters = tuple(group(t) for t in traces)
    synthetic = np.array([t.synthetic for t in traces], dtype=np.bool_)

    def pack_scores(pack: str) -> NDArray[np.float64]:
        monitor = ArmourRuleMonitor(pack)
        return np.array([monitor.score(t).score for t in traces], dtype=np.float64)

    single = ScoreSet(
        trace_ids=ids,
        labels=labels,
        scores=pack_scores(MONITOR_PACK),
        clusters=clusters,
        synthetic=synthetic,
    )
    cascade = ScoreSet(
        trace_ids=ids,
        labels=labels,
        scores=pack_scores(STAGE1_PACK),
        clusters=clusters,
        stage2_scores=pack_scores(STAGE2_PACK),
        synthetic=synthetic,
    )
    return {
        "monitor_scores.csv": dumps_scores(
            single,
            "csv",
            (*HEADER, f"score: Armour rule pack '{MONITOR_PACK}' (baserate.monitors.rules)."),
        ),
        "cascade_scores.csv": dumps_scores(
            cascade,
            "csv",
            (
                *HEADER,
                f"score: stage 1, Armour rule pack '{STAGE1_PACK}'; stage2_score: stage 2, "
                f"Armour rule pack '{STAGE2_PACK}' (baserate.monitors.rules).",
            ),
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Write the fixtures, or with --check report whether the committed ones match."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=HERE, help="output directory")
    parser.add_argument(
        "--check", action="store_true", help="compare with the files in --out; write nothing"
    )
    args = parser.parse_args(argv)
    stale = []
    for name, text in build().items():
        path = args.out / name
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if args.check:
            current = path.read_text(encoding="utf-8") if path.is_file() else None
            if current != text:
                stale.append(name)
            print(f"{name}: {'out of date' if current != text else 'up to date'} {digest}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="")
            print(f"wrote {path} {digest}")
    if stale:
        print("run examples/fixtures/make_fixtures.py to rebuild: " + ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
