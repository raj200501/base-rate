"""Experiment E0: coverage of every estimator on the preregistered grid.

    python tools/run_e0.py run prereg/0001-e0-statistical-engine.md [--workers 4]
    python tools/run_e0.py finalize prereg/0001-e0-statistical-engine.md
    python tools/run_e0.py dry-run prereg/0001-e0-statistical-engine.md

``run`` simulates every cell of the frozen grid (resumable: finished cells
are kept under runs/e0/cells/). ``finalize`` aggregates them into the banked
artifact artifacts/e0/summary.json (with bit-packed per-replicate hits so the
coverage can be recounted), writes coverage plots under artifacts/e0/plots/,
the library's unsupported-cell list src/baserate/estimate/unsupported_cells.json,
and docs/UNSUPPORTED.md. ``dry-run`` uses the preregistered dry-run seed and
replicate count, writes only under runs/e0-dry/, and is never pooled with the
confirmatory run.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np

from baserate.estimate import simulate
from baserate.estimate.operating import flag_rate_from_budget

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "artifacts" / "e0" / "summary.json"
PLOTS = ROOT / "artifacts" / "e0" / "plots"
UNSUPPORTED_JSON = ROOT / "src" / "baserate" / "estimate" / "unsupported_cells.json"
UNSUPPORTED_MD = ROOT / "docs" / "UNSUPPORTED.md"


def canonical_sha256(obj: Any) -> str:
    """SHA-256 of canonical JSON."""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load_config(prereg: Path) -> dict[str, Any]:
    """The ```json prereg-config``` block of a preregistration."""
    blocks = re.findall(r"```json prereg-config\n(.*?)```", prereg.read_text(), re.S)
    if len(blocks) != 1:
        raise SystemExit(f"{prereg} needs exactly one prereg-config block")
    config: dict[str, Any] = json.loads(blocks[0])
    return config


def cells(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Grid cells (without the estimator dimension), in a fixed order."""
    g = config["grid"]
    out = []
    for i, (dist, design, prev, budget) in enumerate(
        product(g["score_distributions"], g["sample_sizes"], g["prevalences"], g["budgets_per_10k"])
    ):
        out.append(
            {
                "index": i,
                "score_distribution": dist,
                "sample_size": design,
                "prevalence": prev,
                "budget_per_10k": budget,
            }
        )
    return out


def cell_key(cell: dict[str, Any]) -> str:
    """Stable id of a grid cell (without the estimator)."""
    return (
        f"{cell['score_distribution']}|{cell['sample_size']}|"
        f"p{cell['prevalence']:g}|b{cell['budget_per_10k']:g}"
    )


def _work(args: tuple[dict[str, Any], dict[str, Any], int, int, int, str]) -> dict[str, Any]:
    cell, config, replicates, boot_reps, seed, out_dir = args
    model = simulate.MODELS[cell["score_distribution"]]
    design = simulate.DESIGNS[cell["sample_size"]]
    flag_rate = flag_rate_from_budget(cell["budget_per_10k"])
    seq = np.random.SeedSequence(seed, spawn_key=(cell["index"],))
    start = time.time()
    tr, results = simulate.run_cell(
        model,
        design,
        cell["prevalence"],
        flag_rate,
        tuple(config["grid"]["estimators"]),
        replicates,
        boot_reps,
        config["resamples"],
        seq,
    )
    record = {
        "cell": cell,
        "truth": tr.__dict__,
        "seconds": round(time.time() - start, 1),
        "results": [
            {
                "estimator": r.estimator,
                "replicates": int(r.hits.size),
                "covered": int(r.hits.sum()),
                "hits_b64": base64.b64encode(np.packbits(r.hits).tobytes()).decode(),
                "mean_width": float(r.widths.mean()) if r.widths.size else None,
            }
            for r in results
        ],
    }
    path = Path(out_dir) / f"cell{cell['index']:04d}.json"
    path.write_text(json.dumps(record) + "\n")
    return record


def run(prereg: Path, workers: int, dry: bool) -> None:
    """Simulate every cell not yet done."""
    config = load_config(prereg)
    out_dir = ROOT / "runs" / ("e0-dry" if dry else "e0") / "cells"
    out_dir.mkdir(parents=True, exist_ok=True)
    reps = config["dry_run_replicates"] if dry else config["replicates"]
    boot = config["dry_run_replicates"] if dry else config["bootstrap_replicates"]
    seed = config["dry_run_seed"] if dry else config["seed"]
    todo = [c for c in cells(config) if not (out_dir / f"cell{c['index']:04d}.json").exists()]
    print(f"{len(todo)} cells to run with {workers} workers", flush=True)
    jobs = [(c, config, reps, boot, seed, str(out_dir)) for c in todo]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_work, j) for j in jobs]
        for done, fut in enumerate(as_completed(futures), start=1):
            rec = fut.result()
            print(f"[{done}/{len(jobs)}] {cell_key(rec['cell'])} {rec['seconds']}s", flush=True)


def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def finalize(prereg: Path, run_meta: dict[str, Any]) -> None:
    """Aggregate finished cells into the banked artifact, plots, and support lists."""
    config = load_config(prereg)
    in_dir = ROOT / "runs" / "e0" / "cells"
    grid_cells = cells(config)
    records = []
    for c in grid_cells:
        path = in_dir / f"cell{c['index']:04d}.json"
        if not path.exists():
            raise SystemExit(f"cell {c['index']} not finished: {path}")
        records.append(json.loads(path.read_text()))
    nominal, tol = config["nominal"], config["tolerance"]
    out_cells = []
    for rec in records:
        c = rec["cell"]
        design = simulate.DESIGNS[c["sample_size"]]
        for r in rec["results"]:
            passes = simulate.coverage_passes(r["covered"], r["replicates"], nominal, tol)
            cov = r["covered"] / r["replicates"]
            out_cells.append(
                {
                    "id": f"{r['estimator']}|{cell_key(c)}",
                    "estimator": r["estimator"],
                    "score_distribution": c["score_distribution"],
                    "sample_size": c["sample_size"],
                    "prevalence": c["prevalence"],
                    "budget_per_10k": c["budget_per_10k"],
                    "n_neg": design.n_neg,
                    "n_pos": design.n_pos,
                    "clustered": design.neg_cluster_size > 1,
                    "truth": rec["truth"][r["estimator"].split(":", 1)[0]],
                    "replicates": r["replicates"],
                    "covered": r["covered"],
                    "coverage": cov,
                    "mcse": float(np.sqrt(cov * (1 - cov) / r["replicates"])),
                    "passes": passes,
                    "refits_by_bootstrap": simulate.refits_by_bootstrap(r["estimator"]),
                    "mean_width": r["mean_width"],
                    "hits_b64": r["hits_b64"],
                }
            )
    plots = make_plots(out_cells, config)
    summary = {
        "experiment": "E0",
        "confirmatory": True,
        "prereg": str(prereg.relative_to(ROOT)),
        "prereg_config_sha256": canonical_sha256(config),
        "nominal": nominal,
        "tolerance": tol,
        "cells": out_cells,
        "plots": plots,
        "provenance": {
            "git_sha": _git_sha(),
            "config_sha256": canonical_sha256(config),
            "seed": config["seed"],
            "finalized_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "command": " ".join(sys.argv),
            "run": run_meta,
            "environment": {
                "python": platform.python_version(),
                "numpy": np.__version__,
                "platform": platform.platform(),
                "cpus": os.cpu_count(),
            },
            "models": "none (pure simulation; no model calls)",
            "usage": {"model_calls": 0, "cash_usd": 0},
        },
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(summary, indent=1) + "\n")
    write_support(out_cells, config)
    print(f"wrote {ARTIFACT.relative_to(ROOT)} with {len(out_cells)} estimator cells")


def make_plots(out_cells: list[dict[str, Any]], config: dict[str, Any]) -> list[str]:
    """One coverage plot per metric family; returns repository-relative paths."""
    import matplotlib

    matplotlib.use("svg")
    import matplotlib.pyplot as plt

    plt.rcParams["svg.hashsalt"] = "baserate-e0"
    PLOTS.mkdir(parents=True, exist_ok=True)
    nominal, tol = config["nominal"], config["tolerance"]
    paths = []
    for metric in ("budget_recall", *simulate.FIXED_METRICS):
        ests = [e for e in config["grid"]["estimators"] if e.startswith(metric + ":")]
        fig, ax = plt.subplots(figsize=(8, 0.6 + 0.45 * len(ests)))
        for i, est in enumerate(ests):
            cs = [c for c in out_cells if c["estimator"] == est]
            cov = np.array([c["coverage"] for c in cs])
            colors = ["#2a7" if c["passes"] else "#c33" for c in cs]
            jitter = (np.arange(len(cs)) % 7 - 3) * 0.04
            ax.scatter(cov, i + jitter, s=9, c=colors, linewidths=0)
        ax.axvline(nominal, color="#333", lw=1)
        ax.axvline(nominal - tol, color="#999", lw=1, ls="--")
        ax.set_yticks(range(len(ests)), [e.split(":", 1)[1] for e in ests])
        ax.set_xlabel("simulated coverage of nominal 95% interval (one dot per grid cell)")
        ax.set_title(f"E0 coverage: {metric} (green passes the preregistered criterion)")
        ax.set_xlim(max(0.0, min(0.5, ax.get_xlim()[0])), 1.0)
        fig.tight_layout()
        path = PLOTS / f"coverage_{metric}.svg"
        fig.savefig(path, metadata={"Date": None})
        plt.close(fig)
        paths.append(str(path.relative_to(ROOT)))
    return paths


def write_support(out_cells: list[dict[str, Any]], config: dict[str, Any]) -> None:
    """Library warning list and docs table of failing cells."""
    failing = [c for c in out_cells if not c["passes"]]
    lib_cells = [
        {
            "id": c["id"],
            "metric": c["estimator"].split(":", 1)[0],
            "estimator": c["estimator"].split(":", 1)[1],
            "prevalence": c["prevalence"],
            "flag_rate": flag_rate_from_budget(c["budget_per_10k"]),
            "n_neg": c["n_neg"],
            "n_pos": c["n_pos"],
            "clustered": c["clustered"],
            "truth": c["truth"],
            "coverage": c["coverage"],
        }
        for c in failing
    ]
    UNSUPPORTED_JSON.write_text(
        json.dumps(
            {
                "schema": "baserate-unsupported/1",
                "source": "artifacts/e0/summary.json",
                "cells": lib_cells,
            },
            indent=1,
        )
        + "\n"
    )
    lines = [
        "# Unsupported configurations",
        "",
        "Generated by `python tools/run_e0.py finalize` from artifacts/e0/summary.json.",
        "Each row is",
        "an E0 cell where the interval method's simulated coverage fell below the preregistered",
        "criterion (nominal minus 1.5 points minus twice the Monte Carlo standard error). The",
        "library emits `UnsupportedCellWarning` for calls in a matching configuration.",
        "",
        "| Cell id | Coverage | Replicates |",
        "|---|---|---|",
    ]
    lines += [f"| `{c['id']}` | {c['coverage']:.4f} | {c['replicates']} |" for c in failing]
    if not failing:
        lines.append("| (none) | | |")
    UNSUPPORTED_MD.write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("command", choices=["run", "finalize", "dry-run"])
    parser.add_argument("prereg", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--run-meta", type=Path, help="JSON file describing the run (for provenance)"
    )
    args = parser.parse_args(argv)
    prereg = args.prereg.resolve()
    if args.command == "finalize":
        meta = json.loads(args.run_meta.read_text()) if args.run_meta else {}
        finalize(prereg, meta)
    else:
        run(prereg, args.workers, dry=args.command == "dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
