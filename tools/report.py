"""Build every figure, table, and results document from banked artifacts (``make report``).

Reads artifacts/e0/summary.json, artifacts/corpus/manifest.json,
artifacts/corpus/label_audit.json, artifacts/e2/results.json,
artifacts/e3/results.json, artifacts/e4/results.json, and (if present)
artifacts/certify/validation.json, and writes:

- SVG figures under artifacts/report/figures/ (fixed hash salt and no date,
  so rebuilds are byte-identical);
- Markdown tables under artifacts/report/tables/, each opening with the
  analysis set, its synthetic share, and its number of independent source runs;
- artifacts/report/numbers.json: derived values (shares, counts, differences)
  that prose cites, because the claim grammar has no arithmetic;
- artifacts/report/manifest.json: every generated figure and table with hashes;
- docs/claims.json and the templates docs/templates/README.md.tmpl and
  docs/templates/REPORT.md.tmpl, built from the hand-written prose sources
  docs/templates/README.prose.md and docs/templates/REPORT.prose.md by
  replacing each ``<!-- @table NAME -->`` marker with a claim-tagged table;
- README.md and docs/REPORT.md, rendered by ``tools/claims.py render``.

No number is typed: table cells are claims selected from the results
artifacts, prose numbers are claims over the same artifacts or over
numbers.json, and parameter values carry allowance tags.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("svg")
import matplotlib.pyplot as plt
import numpy as np

REPO = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("BASERATE_ROOT", REPO))
"""Data root; tests point it at a temporary tree."""
OUT = ROOT / "artifacts" / "report"
FIG = OUT / "figures"
TAB = OUT / "tables"
TEMPLATES = ROOT / "docs" / "templates"

# Validated categorical slots (light mode, all-pairs safe for three series),
# ink, surface, and status colors from the reference palette.
SERIES = ("#2a78d6", "#eb6834", "#1baf7a")
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
GOOD, CRITICAL = "#0ca30c", "#d03b3b"
DIVERGING = ("#2a78d6", "#f0efec", "#e34948")
MARKERS = ("o", "s", "D")
FAMILY_ORDER = ("rule", "model", "ensemble", "cascade")


def style() -> None:
    """Plain, deterministic chart style."""
    plt.rcParams.update(
        {
            "svg.hashsalt": "baserate-report",
            "svg.fonttype": "none",
            "font.family": "DejaVu Sans",
            "font.size": 9,
            "axes.edgecolor": INK2,
            "axes.labelcolor": INK,
            "axes.titlecolor": INK,
            "axes.titlesize": 10,
            "xtick.color": INK2,
            "ytick.color": INK2,
            "axes.grid": True,
            "grid.color": GRID,
            "grid.linewidth": 0.8,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "legend.frameon": False,
            "lines.linewidth": 2,
        }
    )


def load(rel: str) -> Any:
    """A JSON artifact."""
    return json.loads((ROOT / rel).read_text())


def sha(path: Path) -> str:
    """Hex SHA-256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(fig: Any, name: str) -> str:
    """Write a figure; return its repository-relative path."""
    FIG.mkdir(parents=True, exist_ok=True)
    path = FIG / f"{name}.svg"
    fig.savefig(path, metadata={"Date": None})
    plt.close(fig)
    return str(path.relative_to(ROOT))


def short(monitor: str) -> str:
    """Display name of a monitor id."""
    if monitor.startswith("ensemble:"):
        return "ensemble (rank mean)"
    if monitor.startswith("cascade:"):
        return "cascade 0.6b>8b"
    return (
        monitor.split(":", 1)[1].replace("armour-", "").replace("-q8_0", "").replace("-q4_k_m", "")
    )


def fmt_prev(p: float) -> str:
    """1e-03 style text for a prevalence."""
    return f"{p:.0e}".replace("e-0", "e-")


# ---------------------------------------------------------------- claims


class Registry:
    """Claims registered while building the templates."""

    def __init__(self) -> None:
        self.claims: dict[str, dict[str, str]] = {}

    def tag(self, cid: str, source: str, select: str, fmt: str) -> str:
        """Register (or reuse) a claim and return its template tag."""
        entry = {"source": source, "select": select, "format": fmt}
        if cid in self.claims and self.claims[cid] != entry:
            raise SystemExit(f"claim {cid} registered twice with different extractors")
        self.claims[cid] = entry
        return f"{{{{claim:{cid}}}}}"


def p(text: object) -> str:
    """A parameter allowance tag."""
    return f"{{{{param:{text}}}}}"


def cid_part(text: str) -> str:
    """Make text safe inside a claim id."""
    return re.sub(r"[^A-Za-z0-9_.\-]", "-", text)


# ---------------------------------------------------------------- figures


def primary_monitors(e2: dict[str, Any]) -> list[tuple[str, str]]:
    """(family, monitor) pairs scored on the primary set, in display order."""
    seen: dict[str, str] = {}
    for e in e2["estimates"]:
        if e["set"] == "primary":
            seen.setdefault(e["monitor"], e["family"])
    order = sorted(seen.items(), key=lambda kv: (FAMILY_ORDER.index(kv[1]), kv[0]))
    return [(fam, mon) for mon, fam in order]


def est(e2: dict[str, Any], **want: Any) -> dict[str, Any]:
    """The one E2 estimate matching all fields."""
    hits = [e for e in e2["estimates"] if all(e.get(k) == v for k, v in want.items())]
    if len(hits) != 1:
        raise SystemExit(f"expected one estimate for {want}, found {len(hits)}")
    found: dict[str, Any] = hits[0]
    return found


def fig_budget_panels(e2: dict[str, Any], metric: str, name: str, log: bool) -> str:
    """Dot plot with intervals: one panel per budget, rows are monitors, color is prevalence."""
    mons = primary_monitors(e2)
    budgets = sorted({e["budget"] for e in e2["estimates"]})
    prevs = sorted({e["prevalence"] for e in e2["estimates"]}, reverse=True)
    fig, axes = plt.subplots(1, len(budgets), figsize=(10, 0.42 * len(mons) + 1.4), sharey=True)
    for ax, b in zip(np.atleast_1d(axes), budgets, strict=True):
        for j, pv in enumerate(prevs):
            ys = np.arange(len(mons)) + (j - 1) * 0.22
            vals = [
                est(e2, monitor=m, set="primary", prevalence=pv, budget=b, metric=metric)
                for _, m in mons
            ]
            x = np.array([v["estimate"] for v in vals])
            lo = np.array([v["ci_low"] for v in vals])
            hi = np.array([v["ci_high"] for v in vals])
            ax.hlines(ys, lo, hi, color=SERIES[j], linewidth=2)
            ax.plot(
                x,
                ys,
                MARKERS[j],
                color=SERIES[j],
                markersize=6,
                markeredgecolor=SURFACE,
                markeredgewidth=1,
                label=f"prevalence {fmt_prev(pv)}",
            )
        ax.set_title(f"{b} reviews per 10,000")
        if log:
            ax.set_xscale("log")
            ax.set_xlim(1e-3, 1.05)
        else:
            ax.set_xlim(-0.02, 1.02)
        ax.set_xlabel(metric.replace("_at_budget", "").replace("_", " ") + " (95% interval)")
    ax0 = np.atleast_1d(axes)[0]
    ax0.set_yticks(np.arange(len(mons)), [f"{short(m)}  [{f}]" for f, m in mons])
    ax0.invert_yaxis()
    np.atleast_1d(axes)[-1].legend(loc="lower right", fontsize=8)
    fig.tight_layout()
    return save(fig, name)


def fig_ladder(e2: dict[str, Any]) -> str:
    """Recall against model size for the ladder, with the best rule monitor as a reference."""
    lad = e2["size_ladder"]
    fig, ax = plt.subplots(figsize=(5.5, 3.4))
    x = [r["params_b"] for r in lad["rungs"]]
    y = [r["recall"] for r in lad["rungs"]]
    lo = [r["ci_low"] for r in lad["rungs"]]
    hi = [r["ci_high"] for r in lad["rungs"]]
    ax.vlines(x, lo, hi, color=SERIES[0], linewidth=2)
    ax.plot(
        x,
        y,
        "-o",
        color=SERIES[0],
        markersize=7,
        markeredgecolor=SURFACE,
        label="Qwen3 monitor (frozen prompt)",
    )
    ax.axhline(
        lad["best_rule_recall"],
        color=SERIES[1],
        linewidth=2,
        linestyle="--",
        label=f"best rule monitor ({short(lad['best_rule_monitor'])})",
    )
    ax.set_xscale("log")
    ax.set_xticks(x, [f"{v:g}B" for v in x])
    ax.minorticks_off()
    ax.set_ylim(-0.02, 1.02)
    ax.set_xlabel("monitor parameters")
    ax.set_ylabel("recall (95% interval)")
    ax.set_title(
        f"Recall at {lad['budget']} reviews per 10,000, prevalence {fmt_prev(lad['prevalence'])}"
    )
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    return save(fig, "size_ladder")


def _jitter(n: int) -> Any:
    """Small vertical offsets so overlapping marks stay visible."""
    return (np.arange(n) % 5 - 2) * 0.07


def fig_e0(e0: dict[str, Any]) -> str:
    """Simulated coverage per interval method; failing cells marked."""
    ests = sorted({c["estimator"] for c in e0["cells"]})
    fig, ax = plt.subplots(figsize=(8, 0.32 * len(ests) + 1.2))
    for i, est_name in enumerate(ests):
        cells = [c for c in e0["cells"] if c["estimator"] == est_name]
        ok = [c for c in cells if c["passes"]]
        bad = [c for c in cells if not c["passes"]]
        ax.plot(
            [c["coverage"] for c in ok],
            i + _jitter(len(ok)),
            "o",
            color=GOOD,
            markersize=3,
            label="passes" if i == 0 else None,
        )
        ax.plot(
            [c["coverage"] for c in bad],
            i + _jitter(len(bad)),
            "x",
            color=CRITICAL,
            markersize=4,
            label="below criterion" if i == 0 else None,
        )
    nominal, tol = e0["nominal"], e0["tolerance"]
    ax.axvline(nominal, color=INK2, linewidth=1)
    ax.axvline(nominal - tol, color=INK2, linewidth=1, linestyle="--")
    ax.set_yticks(range(len(ests)), ests)
    ax.invert_yaxis()
    ax.set_xlim(max(0.0, min(c["coverage"] for c in e0["cells"]) - 0.02), 1.0)
    ax.set_xlabel("simulated coverage of a nominal 95% interval (one mark per grid cell)")
    ax.legend(loc="lower left", fontsize=8)
    fig.tight_layout()
    return save(fig, "e0_coverage")


def fig_cascades(e4: dict[str, Any]) -> str:
    """Cost-recall fronts per cascade pair, with the held-out result of the chosen cascade."""
    pairs = e4["pairs"]
    fig, axes = plt.subplots(1, len(pairs), figsize=(3.2 * len(pairs), 3.2), sharey=True)
    for ax, pr in zip(np.atleast_1d(axes), pairs, strict=True):
        front = pr["front"]
        ax.plot(
            [f["cost"] for f in front],
            [f["recall"] for f in front],
            "-o",
            color=SERIES[0],
            markersize=4,
            label="front (whole primary set)",
        )
        lo, hi = pr["test_recall_ci"]
        ax.vlines([pr["test_cost"]], [lo], [hi], color=SERIES[1], linewidth=2)
        ax.plot(
            [pr["test_cost"]],
            [pr["test_recall"]],
            "s",
            color=SERIES[1],
            markersize=7,
            markeredgecolor=SURFACE,
            label="chosen cascade, test half",
        )
        ax.axvline(pr["max_cost"], color=INK2, linewidth=1, linestyle="--")
        ax.set_title(f"{short(pr['stage1'])} > {short(pr['stage2'])}", fontsize=9)
        ax.set_xlabel("seconds of monitor compute per trajectory")
        ax.set_ylim(-0.02, 1.02)
    np.atleast_1d(axes)[0].set_ylabel("recall")
    np.atleast_1d(axes)[0].legend(loc="lower right", fontsize=7)
    fig.tight_layout()
    return save(fig, "cascade_fronts")


def fig_transfer(e3: dict[str, Any]) -> str:
    """Realized-over-intended review rate for each (source, target) domain pair."""
    from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm

    mons = sorted({r["monitor"] for r in e3["domain_pairs"]})
    doms = sorted({r["source"] for r in e3["domain_pairs"]})
    cmap = LinearSegmentedColormap.from_list("div", DIVERGING)
    fig, axes = plt.subplots(1, len(mons), figsize=(5 * len(mons), 4.4))
    for ax, m in zip(np.atleast_1d(axes), mons, strict=True):
        grid = np.full((len(doms), len(doms)), np.nan)
        for r in e3["domain_pairs"]:
            if r["monitor"] == m:
                grid[doms.index(r["source"]), doms.index(r["target"])] = np.log2(
                    max(r["ratio"], 1e-3)
                )
        im = ax.imshow(grid, cmap=cmap, norm=TwoSlopeNorm(vcenter=0.0, vmin=-4, vmax=4))
        ax.set_xticks(range(len(doms)), doms, rotation=60, ha="right")
        ax.set_yticks(range(len(doms)), doms)
        ax.set_xlabel("target domain")
        ax.set_ylabel("source domain (threshold tuned here)")
        ax.set_title(short(m))
        ax.grid(False)
    cbar = fig.colorbar(im, ax=list(np.atleast_1d(axes)), shrink=0.8)
    cbar.set_label("log2 of realized / intended review rate")
    return save(fig, "transfer")


# ---------------------------------------------------------------- tables


def caption(set_name: str, comp: dict[str, Any], tags: tuple[str, str] | None = None) -> str:
    """The required caption: analysis set, synthetic share, independent source runs."""
    if tags is None:
        return (
            f"Set: {set_name}. Synthetic share: {comp['synthetic_share']:.3f}. "
            f"Independent source runs: {comp['independent_source_runs']:,}."
        )
    return f"Set: {set_name}. Synthetic share: {tags[0]}. Independent source runs: {tags[1]}."


def set_tags(reg: Registry, source: str, path: str) -> tuple[str, str]:
    """Claim tags for a set's synthetic share and independent source runs."""
    base = cid_part(f"{source}.{path}")
    return (
        reg.tag(f"{base}.synthetic_share", source, f"{path}.synthetic_share", "{:.3f}"),
        reg.tag(f"{base}.runs", source, f"{path}.independent_source_runs", "{:,d}"),
    )


def table_e2(reg: Registry, e2: dict[str, Any], metric: str) -> tuple[str, str]:
    """Headline table: rows monitors, columns prevalence by budget."""
    src = "artifacts/e2/results.json"
    prevs = sorted({e["prevalence"] for e in e2["estimates"]}, reverse=True)
    budgets = sorted({e["budget"] for e in e2["estimates"]})
    cols = [(pv, b) for pv in prevs for b in budgets]
    head_t = (
        "| Monitor | Family | "
        + " | ".join(f"prev. {p(fmt_prev(pv))}, {p(b)}/{p('10k')}" for pv, b in cols)
        + " |"
    )
    head_r = (
        "| Monitor | Family | "
        + " | ".join(f"prev. {fmt_prev(pv)}, {b}/10k" for pv, b in cols)
        + " |"
    )
    sep = "|" + "---|" * (len(cols) + 2)
    rows_t, rows_r = [], []
    for fam, m in primary_monitors(e2):
        cells_t, cells_r = [], []
        for pv, b in cols:
            e = est(e2, monitor=m, set="primary", prevalence=pv, budget=b, metric=metric)
            sel_key = f"family={fam}" if fam == "ensemble" else f"monitor={m}"
            sel = f"estimates[{sel_key},set=primary,prevalence={pv:g},budget={b},metric={metric}]"
            cid = cid_part(f"e2.{short(m)}.{metric}.p{pv:g}.b{b}")
            tag = reg.tag(cid, src, sel, "{estimate:.3f} [{ci_low:.3f}, {ci_high:.3f}]")
            mark = "" if e["headline"] else " (not E0-supported)"
            cells_t.append(tag + mark)
            cells_r.append(f"{e['estimate']:.3f} [{e['ci_low']:.3f}, {e['ci_high']:.3f}]{mark}")
        name_t = f"`{m}`" if not re.search(r"\d", m) else f"{{{{name:{m}}}}}"
        rows_t.append(f"| {name_t} | {fam} | " + " | ".join(cells_t) + " |")
        rows_r.append(f"| `{m}` | {fam} | " + " | ".join(cells_r) + " |")
    comp = e2["sets"]["primary"]
    cap_t = caption("primary", comp, set_tags(reg, src, "sets.primary"))
    cap_r = caption("primary", comp)
    title = metric.replace("_", " ")
    tmpl = "\n".join([f"*{title}, estimate [95% interval]. {cap_t}*", "", head_t, sep, *rows_t])
    rend = "\n".join([f"*{title}, estimate [95% interval]. {cap_r}*", "", head_r, sep, *rows_r])
    return tmpl, rend


def write_table(name: str, rendered: str) -> str:
    """Bank a rendered table; return its path."""
    TAB.mkdir(parents=True, exist_ok=True)
    path = TAB / f"{name}.md"
    path.write_text(rendered + "\n")
    return str(path.relative_to(ROOT))


# ---------------------------------------------------------------- documents


CITE_RE = re.compile(r"\[@([A-Za-z0-9][\w\-]*)\]")


def references(keys: Sequence[str]) -> str:
    """Reference list (template syntax) for cited keys, from artifacts/citecheck.json."""
    data = load("artifacts/citecheck.json")
    entries = {r["key"]: r for r in data["references"]}
    lines = []
    for key in keys:
        ref = entries.get(key)
        if ref is None or not ref.get("verified"):
            raise SystemExit(f"cited reference {key} is missing or unverified in citecheck.json")
        claimed = ref.get("claimed", {})
        title = ref.get("page_title") or claimed.get("title") or key
        year = claimed.get("year")
        when = f" {{{{date:{year}}}}}." if year else ""
        title_t = _tag_digits(str(title))
        lines.append(f"- [@{key}] {title_t}.{when} <{ref['final_url']}>")
    return "\n".join(lines)


def _tag_digits(text: str) -> str:
    """Wrap digit-bearing words of a title in label allowances."""
    return " ".join(f"{{{{label:{w}}}}}" if re.search(r"\d", w) else w for w in text.split())


def build_templates(fragments: dict[str, str]) -> list[str]:
    """Expand ``<!-- @table NAME -->`` markers in the prose sources into templates.

    Citations ``[@key]`` are wrapped in name allowances (keys carry years), and a
    ``<!-- @references -->`` marker becomes the reference list of every key the
    document cites.
    """
    docs = []
    for doc, prose, tmpl in (
        ("README.md", "README.prose.md", "README.md.tmpl"),
        ("docs/REPORT.md", "REPORT.prose.md", "REPORT.md.tmpl"),
    ):
        src = TEMPLATES / prose
        if not src.exists():
            continue
        text = src.read_text()

        def sub(m: re.Match[str], prose: str = prose) -> str:
            name = m.group(1)
            if name not in fragments:
                raise SystemExit(f"{prose}: unknown table {name}")
            return fragments[name]

        text = re.sub(r"<!-- @table ([\w\-]+) -->", sub, text)
        cited = list(dict.fromkeys(CITE_RE.findall(text)))
        text = text.replace("<!-- @references -->", references(cited) if cited else "")
        text = CITE_RE.sub(lambda m: f"{{{{name:[@{m.group(1)}]}}}}", text)
        (TEMPLATES / tmpl).write_text(text)
        docs.append(doc)
    return docs


def main(argv: Sequence[str] | None = None) -> int:
    """Build everything; render the documents."""
    style()
    reg = Registry()
    figures: list[str] = []
    tables: list[str] = []
    fragments: dict[str, str] = {}
    builders: list[tuple[str, Callable[[], None]]] = []

    e0 = load("artifacts/e0/summary.json")
    figures.append(fig_e0(e0))
    e2 = load("artifacts/e2/results.json")
    figures.append(fig_budget_panels(e2, "recall_at_budget", "headline_recall", log=False))
    figures.append(fig_budget_panels(e2, "precision_at_budget", "precision", log=True))
    figures.append(fig_ladder(e2))
    for metric in ("recall_at_budget", "precision_at_budget"):
        t, r = table_e2(reg, e2, metric)
        fragments[f"e2-{metric}"] = t
        tables.append(write_table(f"e2_{metric}", r))
    if (ROOT / "artifacts" / "e3" / "results.json").exists():
        figures.append(fig_transfer(load("artifacts/e3/results.json")))
    if (ROOT / "artifacts" / "e4" / "results.json").exists():
        figures.append(fig_cascades(load("artifacts/e4/results.json")))
    del builders
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {
        "figures": figures,
        "tables": tables,
        "hashes": {f: sha(ROOT / f) for f in figures + tables},
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    docs = build_templates(fragments)
    registry = {
        "schema": "baserate-claims/1",
        "documents": {d: f"docs/templates/{Path(d).name}.tmpl" for d in docs},
        "claims": dict(sorted(reg.claims.items())),
    }
    (ROOT / "docs" / "claims.json").write_text(json.dumps(registry, indent=1) + "\n")
    if docs:
        subprocess.run(
            [sys.executable, str(REPO / "tools" / "claims.py"), "--root", str(ROOT), "render"],
            cwd=ROOT,
            check=True,
        )
    print(
        f"report: {len(figures)} figures, {len(tables)} tables, {len(reg.claims)} claims, "
        f"documents: {', '.join(docs) or 'none'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
