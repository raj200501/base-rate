"""Rebuild every result from the banked artifacts and check that nothing changes.

``make reproduce`` runs this. It makes no model calls and needs no network:

1. Re-runs the E2, E3, and E4 analyses (tools/analyze.py) from the banked
   corpus shards and score files in a scratch data root, and checks that each
   recomputed results file equals the banked one in every field except
   ``provenance`` (which records when and from which commit it ran).
2. Re-runs tools/report.py in the repository and checks with git that no
   generated figure, table, derived number, template, claim registry, or
   rendered document changed.

E0 (the simulation study, hours of CPU) and the corpus build (which needs the
raw runs under runs/) are not re-run here; tools/run_e0.py and
tools/build_corpus.py document how to repeat them. The last stdout line is
``REPRODUCE: PASS <n> checks`` or ``REPRODUCE: FAIL <n> problems``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("BASERATE_ROOT", TOOLS.parent))
"""Data root (a git work tree); tests point it at a temporary repository."""
ANALYSES = ("e2", "e3", "e4")
"""Each re-runs under the preregistration its banked results file cites."""
SHARED = ("corpus", "scores", "e0")
"""Banked inputs the analyses read; linked into the scratch root, never copied."""
GENERATED = (
    "artifacts/report",
    "docs/claims.json",
    "docs/templates",
    "README.md",
    "docs/REPORT.md",
    "docs/DATA.md",
)


def without_provenance(result: dict[str, Any]) -> dict[str, Any]:
    """A results artifact without the fields that record when and where it ran."""
    return {k: v for k, v in result.items() if k != "provenance"}


def first_difference(a: Any, b: Any, at: str = "") -> str | None:
    """The JSON path of the first difference between two values, or None."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                return f"{at}.{key}"
            found = first_difference(a[key], b[key], f"{at}.{key}")
            if found:
                return found
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{at} (length {len(a)} vs {len(b)})"
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            found = first_difference(x, y, f"{at}[{i}]")
            if found:
                return found
        return None
    return None if a == b and type(a) is type(b) else at or "."


def scratch_root(tmp: Path) -> Path:
    """A data root that shares the banked inputs and has empty result directories."""
    root = tmp / "root"
    (root / "artifacts").mkdir(parents=True)
    shutil.copytree(ROOT / "prereg", root / "prereg")
    for name in SHARED:
        if (ROOT / "artifacts" / name).exists():
            (root / "artifacts" / name).symlink_to((ROOT / "artifacts" / name).resolve())
    return root


def check_analyses(problems: list[str]) -> int:
    """Re-run each banked analysis in a scratch root; return the number checked."""
    checked = 0
    with tempfile.TemporaryDirectory(prefix="baserate-reproduce-") as tmp:
        root = scratch_root(Path(tmp))
        env = {**os.environ, "BASERATE_ROOT": str(root), "BASERATE_NO_MODEL_CALLS": "1"}
        for exp in ANALYSES:
            banked_path = ROOT / "artifacts" / exp / "results.json"
            if not banked_path.exists():
                problems.append(f"artifacts/{exp}/results.json is not banked")
                continue
            banked_full = json.loads(banked_path.read_text())
            prereg = str(banked_full.get("prereg", ""))
            if not prereg.startswith("prereg/") or not (root / prereg).is_file():
                problems.append(f"{exp}: banked results cite no preregistration in prereg/")
                continue
            run = subprocess.run(
                [sys.executable, str(TOOLS / "analyze.py"), exp, str(root / prereg)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if run.returncode != 0:
                problems.append(f"{exp}: analysis failed: {(run.stderr or run.stdout)[-400:]}")
                continue
            banked = without_provenance(banked_full)
            fresh = without_provenance(
                json.loads((root / "artifacts" / exp / "results.json").read_text())
            )
            diff = first_difference(banked, fresh)
            if diff is not None:
                problems.append(f"{exp}: recomputed results differ from the banked ones at {diff}")
            checked += 1
    return checked


def check_report(problems: list[str]) -> int:
    """Rebuild the report in place; nothing generated may change."""
    run = subprocess.run(
        [sys.executable, str(TOOLS / "report.py")],
        cwd=ROOT,
        env={**os.environ, "BASERATE_ROOT": str(ROOT), "BASERATE_NO_MODEL_CALLS": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        problems.append(f"report: failed: {(run.stderr or run.stdout)[-400:]}")
        return 0
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *GENERATED],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    for line in status:
        problems.append(f"report: rebuilt output differs from the committed one: {line[3:]}")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--skip-analyses", action="store_true", help="only rebuild the report")
    args = ap.parse_args(argv)
    problems: list[str] = []
    checks = 0 if args.skip_analyses else check_analyses(problems)
    checks += check_report(problems)
    for problem in problems:
        print(f"- {problem}")
    if problems:
        print(f"REPRODUCE: FAIL {len(problems)} problems")
        return 1
    print(f"REPRODUCE: PASS {checks} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
