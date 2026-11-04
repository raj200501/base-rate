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

import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("svg")
import matplotlib.pyplot as plt
import numpy as np

REPO = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("BASERATE_ROOT", REPO)).resolve()
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
    """Plain, deterministic chart style, independent of any local matplotlibrc."""
    matplotlib.rcdefaults()
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


ENSEMBLE_RE = re.compile(r"^ensemble:([\w-]+)\((.+)\)$")
CASCADE_RE = re.compile(r"^cascade:(.+)>(.+)@([0-9.]+)$")


def short(monitor: str) -> str:
    """Short display name of a monitor id (figure labels and claim ids)."""
    m = ENSEMBLE_RE.match(monitor)
    if m:
        return f"ensemble ({m.group(1).replace('-', ' ')})"
    m = CASCADE_RE.match(monitor)
    if m:
        return f"cascade {short(m.group(1))}>{short(m.group(2))}"
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

    def tag(self, cid: str, source: str, select: str, fmt: str, note: str = "") -> str:
        """Register (or reuse) a claim and return its template tag."""
        entry = {"source": source, "select": select, "format": fmt}
        old = {k: v for k, v in self.claims.get(cid, {}).items() if k != "note"}
        if old and old != entry:
            raise SystemExit(f"claim {cid} registered twice with different extractors")
        self.claims[cid] = {**entry, **({"note": note} if note else {})}
        return f"{{{{claim:{cid}}}}}"

    def add_prose_claims(self, path: Path) -> None:
        """Register the hand-written claims that prose cites (docs/templates/claims.prose.json)."""
        if not path.exists():
            return
        for cid, raw in json.loads(path.read_text()).items():
            unknown = set(raw) - {"source", "select", "format", "note"}
            if unknown:
                raise SystemExit(f"{path.name}: claim {cid} has unknown keys {sorted(unknown)}")
            self.tag(
                cid,
                raw["source"],
                raw.get("select", ""),
                raw.get("format", "{}"),
                raw.get("note", ""),
            )


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
    shown = [
        v
        for e in e2["estimates"]
        if e["set"] == "primary" and e["metric"] == metric
        for v in (e["estimate"], e["ci_low"], e["ci_high"])
        if v > 0
    ]
    # a log axis cannot show zero: its lower limit sits below every positive value, and
    # zero estimates and interval ends are drawn at that limit
    lo_log = min([1e-3, *shown]) / 2 if log else 0.0
    has_zero = log and any(
        v <= 0
        for e in e2["estimates"]
        if e["set"] == "primary" and e["metric"] == metric
        for v in (e["estimate"], e["ci_low"])
    )
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
            if log:
                x, lo, hi = (np.maximum(arr, lo_log) for arr in (x, lo, hi))
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
            ax.set_xlim(lo_log, 1.05)
        else:
            ax.set_xlim(-0.02, 1.02)
        ax.set_xlabel(metric.replace("_at_budget", "").replace("_", " ") + " (95% interval)")
    ax0 = np.atleast_1d(axes)[0]
    ax0.set_yticks(np.arange(len(mons)), [f"{short(m)}  [{f}]" for f, m in mons])
    ax0.invert_yaxis()
    np.atleast_1d(axes)[-1].legend(loc="lower right", fontsize=8)
    if has_zero:
        fig.text(
            0.01,
            0.01,
            "zero values are drawn at the left edge of each panel",
            fontsize=7,
            color=INK2,
        )
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

sys.path.insert(0, str(REPO / "tools"))
import claims as claimlib  # noqa: E402

TAG_RE = re.compile(r"\{\{([a-z]+):([^{}\n]*)\}\}")
NUMBERS = "artifacts/report/numbers.json"
E2_SRC = "artifacts/e2/results.json"
CI3 = "{estimate:.3f} [{ci_low:.3f}, {ci_high:.3f}]"


def render_fragment(reg: Registry, text: str) -> str:
    """Render a template fragment exactly as tools/claims.py renders a document."""
    parsed: dict[str, Any] = {}

    def sub(m: re.Match[str]) -> str:
        kind, body = m.group(1), m.group(2)
        if kind != "claim":
            return body
        entry = reg.claims[body]
        src = entry["source"]
        if src not in parsed:
            parsed[src] = claimlib.parse_source(src, (ROOT / src).read_bytes())
        value = claimlib.select(parsed[src], claimlib.parse_selector(entry["select"]))
        return claimlib.render_value(value, claimlib.parse_format(entry["format"]))

    return TAG_RE.sub(sub, text)


def caption(set_name: str, tags: tuple[str, str, str]) -> str:
    """The required caption: the set, its traces and independent runs, its synthetic share."""
    traces, runs, synthetic = tags
    return (
        f"Set: {set_name}. Traces: {traces}. Independent source runs: {runs}. "
        f"Synthetic share: {synthetic}."
    )


def set_tags(reg: Registry, source: str, path: str) -> tuple[str, str, str]:
    """Claim tags for a set's trace count, independent source runs, and synthetic share."""
    base = cid_part(f"{source}.{path}")
    return (
        reg.tag(f"{base}.traces", source, f"{path}.traces", "{:,d}"),
        reg.tag(f"{base}.runs", source, f"{path}.independent_source_runs", "{:,d}"),
        reg.tag(f"{base}.synthetic_share", source, f"{path}.synthetic_share", "{:.3f}"),
    )


def md_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """A Markdown table."""
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def framed(title: str, cap: str, table: str) -> str:
    """Title and caption line above a table."""
    return f"*{title}. {cap}*\n\n{table}"


def name(text: str) -> str:
    """An identifier in code font, with a name allowance when it holds digits."""
    return f"`{{{{name:{text}}}}}`" if re.search(r"\d", text) else f"`{text}`"


def display(monitor: str) -> str:
    """A monitor id for a table cell; ensembles and cascades are spelled out by member."""
    m = ENSEMBLE_RE.match(monitor)
    if m:
        members = " and ".join(name(x) for x in m.group(2).split(","))
        return f"{m.group(1).replace('-', ' ')} of {members}"
    m = CASCADE_RE.match(monitor)
    if m:
        return f"{name(m.group(1))} then {name(m.group(2))}, escalating {p(m.group(3))}"
    return name(monitor)


def ci_tag(reg: Registry, cid: str, source: str, select: str, lo_hi: str = "") -> str:
    """Tag for ``estimate [low, high]`` (or a value with a ``_ci`` list beside it)."""
    if not lo_hi:
        return reg.tag(cid, source, select, CI3)
    value = reg.tag(cid, source, select, "{:.3f}")
    low = reg.tag(f"{cid}.lo", source, f"{lo_hi}[0]", "{:.3f}")
    high = reg.tag(f"{cid}.hi", source, f"{lo_hi}[1]", "{:.3f}")
    return f"{value} [{low}, {high}]"


def table_e2(reg: Registry, e2: dict[str, Any], metric: str, set_name: str = "primary") -> str:
    """Rows monitors, columns prevalence by budget, for one metric and set."""
    on_set = [e for e in e2["estimates"] if e["set"] == set_name]
    prevs = sorted({e["prevalence"] for e in on_set}, reverse=True)
    budgets = sorted({e["budget"] for e in on_set})
    cols = [(pv, b) for pv in prevs for b in budgets]
    header = ["Monitor", "Family"] + [
        f"prev. {p(fmt_prev(pv))}, {p(b)}/{p('10k')}" for pv, b in cols
    ]
    seen: dict[str, str] = {}
    for e in on_set:
        seen.setdefault(e["monitor"], e["family"])
    mons = sorted(seen.items(), key=lambda kv: (FAMILY_ORDER.index(kv[1]), kv[0]))
    rows = []
    for m, fam in mons:
        cells = [display(m), fam]
        for pv, b in cols:
            e = est(e2, monitor=m, set=set_name, prevalence=pv, budget=b, metric=metric)
            sel_key = f"family={fam}" if fam == "ensemble" else f"monitor={m}"
            sel = (
                f"estimates[{sel_key},set={set_name},prevalence={pv:g},budget={b},metric={metric}]"
            )
            suffix = "" if set_name == "primary" else f".{set_name}"
            cid = cid_part(f"e2.{short(m)}.{metric}.p{pv:g}.b{b}{suffix}")
            mark = "" if e["headline"] or set_name != "primary" else " (not E0-supported)"
            cells.append(ci_tag(reg, cid, E2_SRC, sel) + mark)
        rows.append(cells)
    cap = caption(set_name, set_tags(reg, E2_SRC, f"sets.{set_name}"))
    title = f"{metric.replace('_', ' ')}, estimate [{p('95%')} interval]"
    return framed(title, cap, md_table(header, rows))


def table_headline(reg: Registry, e2: dict[str, Any]) -> str:
    """Small README table: recall and precision at the size-ladder setting."""
    lad = e2["size_ladder"]
    pv, b = lad["prevalence"], lad["budget"]
    rows = []
    for fam, m in primary_monitors(e2):
        sel_key = f"family={fam}" if fam == "ensemble" else f"monitor={m}"
        cells = [display(m), fam]
        for metric in ("recall_at_budget", "precision_at_budget"):
            sel = f"estimates[{sel_key},set=primary,prevalence={pv:g},budget={b},metric={metric}]"
            cells.append(ci_tag(reg, cid_part(f"e2.{short(m)}.{metric}.p{pv:g}.b{b}"), E2_SRC, sel))
        rows.append(cells)
    cap = caption("primary", set_tags(reg, E2_SRC, "sets.primary"))
    title = (
        f"Prevalence {p(fmt_prev(pv))}, {p(b)} reviews per {p('10,000')}, "
        f"estimate [{p('95%')} interval]"
    )
    return framed(title, cap, md_table(["Monitor", "Family", "Recall", "Precision"], rows))


def table_fixed(reg: Registry, e2: dict[str, Any]) -> str:
    """Rule monitors at their fixed threshold on the primary set."""
    rows_in = [r for r in e2["fixed_threshold"] if r["set"] == "primary"]
    if not rows_in:
        return ""
    prevs = sorted({r["prevalence"] for r in rows_in}, reverse=True)
    header = ["Monitor", "Recall", "False-positive rate"] + [
        f"Precision, prev. {p(fmt_prev(pv))}" for pv in prevs
    ]
    rows = []
    for m in sorted({r["monitor"] for r in rows_in}):
        cells = [name(m)]
        for metric in ("recall", "fpr"):
            sel = (
                f"fixed_threshold[monitor={m},set=primary,prevalence={prevs[0]:g},metric={metric}]"
            )
            cells.append(ci_tag(reg, cid_part(f"e2.fixed.{short(m)}.{metric}"), E2_SRC, sel))
        for pv in prevs:
            sel = f"fixed_threshold[monitor={m},set=primary,prevalence={pv:g},metric=precision]"
            cid = cid_part(f"e2.fixed.{short(m)}.precision.p{pv:g}")
            cells.append(ci_tag(reg, cid, E2_SRC, sel))
        rows.append(cells)
    thr = rows_in[0]["threshold"]
    cap = caption("primary", set_tags(reg, E2_SRC, "sets.primary"))
    title = (
        f"Rule monitors at the fixed threshold {{{{threshold:{thr:g}}}}}, "
        f"cluster-aware exact intervals ({p('95%')})"
    )
    return framed(title, cap, md_table(header, rows))


def table_ttd(reg: Registry, e2: dict[str, Any]) -> str:
    """Time to detection among violations whose prefixes were scored."""
    rows = []
    for r in e2["time_to_detection"]:
        m = r["monitor"]
        base = f"time_to_detection[monitor={m}]"
        cid = cid_part(f"e2.ttd.{short(m)}")
        cells = [
            name(m),
            reg.tag(f"{cid}.violations", E2_SRC, f"{base}.violations", "{:,d}"),
            reg.tag(f"{cid}.caught", E2_SRC, f"{base}.share_caught", "{:.3f}")
            if "share_caught" in r
            else "none scored",
        ]
        if "share_detected_at_violating_step" in r:
            cells.append(
                ci_tag(
                    reg,
                    f"{cid}.now",
                    E2_SRC,
                    f"{base}.share_detected_at_violating_step",
                    f"{base}.share_detected_at_violating_step_ci",
                )
            )
        else:
            cells.append("none scored")
        if r.get("median_delay_steps_ci"):
            cells.append(
                ci_tag(
                    reg,
                    f"{cid}.delay",
                    E2_SRC,
                    f"{base}.median_delay_steps",
                    f"{base}.median_delay_steps_ci",
                )
            )
        else:
            cells.append("none caught")
        rows.append(cells)
    if not rows:
        return ""
    r0 = e2["time_to_detection"][0]
    cap = caption("primary", set_tags(reg, E2_SRC, "sets.primary"))
    title = (
        f"Time to detection at prevalence {p(fmt_prev(r0['prevalence']))} and "
        f"{p(r0['budget'])} reviews per {p('10,000')} (bootstrap {p('95%')} intervals)"
    )
    header = [
        "Monitor",
        "Violations scored",
        "Share caught by the end",
        "Share caught at the violating step",
        "Median delay, steps",
    ]
    return framed(title, cap, md_table(header, rows))


def table_breakdown(reg: Registry, e2: dict[str, Any]) -> str:
    """Recall by violation source, violation type, and subject model."""
    rows_in = e2["breakdowns"]
    if not rows_in:
        return ""
    mons = sorted(
        {r["monitor"] for r in rows_in},
        key=lambda m: (FAMILY_ORDER.index(m.split(":", 1)[0]), m),
    )
    groups = [
        (g, v)
        for g in ("violation_source", "violation_type", "subject_model")
        for v in sorted({r["value"] for r in rows_in if r["by"] == g})
    ]
    rows = []
    for g, v in groups:
        n_pos = next(r["n_pos"] for r in rows_in if r["by"] == g and r["value"] == v)
        vid = cid_part(v)
        n_tag = reg.tag(
            f"e2.by.{g}.{vid}.n",
            E2_SRC,
            f"breakdowns[monitor={mons[0]},by={g},value={v}].n_pos",
            "{:,d}",
        )
        del n_pos
        cells = [f"{g.replace('_', ' ')}: {name(v)}", n_tag]
        for m in mons:
            sel = f"breakdowns[monitor={m},by={g},value={v}]"
            cid = cid_part(f"e2.by.{g}.{vid}.{short(m)}")
            cells.append(reg.tag(cid, E2_SRC, sel, "{recall:.2f} [{ci_low:.2f}, {ci_high:.2f}]"))
        rows.append(cells)
    r0 = rows_in[0]
    cap = caption("primary", set_tags(reg, E2_SRC, "sets.primary"))
    title = (
        f"Recall by group at prevalence {p(fmt_prev(r0['prevalence']))} and {p(r0['budget'])} "
        f"reviews per {p('10,000')}, estimate [{p('95%')} interval]"
    )
    header = ["Group", "Violations"] + [name(short(m)) for m in mons]
    return framed(title, cap, md_table(header, rows))


def table_ladder(reg: Registry, e2: dict[str, Any]) -> str:
    """The open-weight size ladder against the best rule monitor."""
    lad = e2["size_ladder"]
    rows = []
    for r in lad["rungs"]:
        m = r["monitor"]
        sel = f"size_ladder.rungs[monitor={m}]"
        cid = cid_part(f"e2.ladder.{short(m)}")
        rows.append(
            [
                name(m),
                p(f"{r['params_b']:g}B"),
                reg.tag(cid, E2_SRC, sel, "{recall:.3f} [{ci_low:.3f}, {ci_high:.3f}]"),
            ]
        )
    bm = lad["best_rule_monitor"]
    sel = (
        f"estimates[monitor={bm},set=primary,prevalence={lad['prevalence']:g},"
        f"budget={lad['budget']},metric=recall_at_budget]"
    )
    best = ci_tag(
        reg,
        cid_part(f"e2.{short(bm)}.recall_at_budget.p{lad['prevalence']:g}.b{lad['budget']}"),
        E2_SRC,
        sel,
    )
    rows.append([name(bm), "rule (best)", best])
    cap = caption("primary", set_tags(reg, E2_SRC, "sets.primary"))
    title = (
        f"Size ladder at prevalence {p(fmt_prev(lad['prevalence']))} and {p(lad['budget'])} "
        f"reviews per {p('10,000')}, recall [{p('95%')} interval]"
    )
    return framed(title, cap, md_table(["Monitor", "Parameters", "Recall"], rows))


def table_e3(reg: Registry, e3: dict[str, Any]) -> str:
    """Every E3 hypothesis with its statistic and outcome."""
    src = "artifacts/e3/results.json"
    rows = []
    for h in e3["hypotheses"]:
        hid = h["id"]
        sel = f"hypotheses[id={hid}]"
        cid = cid_part(f"e3.{hid}")
        if "share" in h:
            stat = "share of domain pairs " + reg.tag(f"{cid}.share", src, f"{sel}.share", "{:.3f}")
        else:
            stat = "review-rate ratio " + ci_tag(
                reg, f"{cid}.ratio", src, f"{sel}.ratio", f"{sel}.ratio_ci"
            )
        outcome = reg.tag(f"{cid}.outcome", src, f"{sel}.outcome", "{}")
        rows.append([name(hid), name(h["monitor"]), stat, outcome])
    cap = caption(E3_SET, set_tags(reg, src, "set_composition"))
    return framed(
        "E3 hypotheses (transfer)",
        cap,
        md_table(["Hypothesis", "Monitor", "Statistic", "Outcome"], rows),
    )


E3_SET = (
    "full corpus: every benign trace, Armour's included, and the natural and inserted "
    "violations (prereg/0004)"
)


def table_e3_pairs(reg: Registry, e3: dict[str, Any]) -> str:
    """E3 domain transfer for every monitor and ordered pair of domains."""
    src = "artifacts/e3/results.json"
    rows = []
    for pr in e3["domain_pairs"]:
        m, a, b = pr["monitor"], pr["source"], pr["target"]
        sel = f"domain_pairs[monitor={m},source={a},target={b}]"
        cid = cid_part(f"e3.pair.{short(m)}.{a}.{b}")
        rows.append(
            [
                name(m),
                name(a),
                name(b),
                ci_tag(reg, f"{cid}.ratio", src, f"{sel}.ratio", f"{sel}.ratio_ci"),
                ci_tag(reg, f"{cid}.gap", src, f"{sel}.recall_gap", f"{sel}.recall_gap_ci"),
            ]
        )
    cap = caption(E3_SET, set_tags(reg, src, "set_composition"))
    title = (
        "E3 domain transfer per ordered pair: realized over intended review rate, and recall "
        f"at the transferred threshold minus recall at the target's own, estimate "
        f"[{p('95%')} interval]"
    )
    header = ["Monitor", "Tuned on", "Applied to", "Review-rate ratio", "Recall gap"]
    return framed(title, cap, md_table(header, rows))


def table_e4(reg: Registry, e4: dict[str, Any]) -> str:
    """Every E4 cascade pair on held-out data, with both hypotheses."""
    src = "artifacts/e4/results.json"
    rows = []
    for pr in e4["pairs"]:
        idx = pr["index"]
        sel = f"pairs[index={idx}]"
        cid = cid_part(f"e4.{idx}")
        rows.append(
            [
                name(idx),
                name(pr["stage1"]),
                name(pr["stage2"]),
                reg.tag(f"{cid}.esc", src, f"{sel}.chosen_escalation_rate", "{:.3f}"),
                ci_tag(reg, f"{cid}.recall", src, f"{sel}.test_recall", f"{sel}.test_recall_ci"),
                ci_tag(
                    reg,
                    f"{cid}.alone",
                    src,
                    f"{sel}.stage2_alone_test_recall",
                    f"{sel}.stage2_alone_test_recall_ci",
                ),
                ci_tag(reg, f"{cid}.cost", src, f"{sel}.test_cost", f"{sel}.test_cost_ci"),
                reg.tag(f"{cid}.max_cost", src, f"{sel}.max_cost", "{:.3f}"),
                reg.tag(f"{cid}.h41", src, f"hypotheses[id=H4.1-{idx}].outcome", "{}"),
                reg.tag(f"{cid}.h42", src, f"hypotheses[id=H4.2-{idx}].outcome", "{}"),
            ]
        )
    first = e4["pairs"][0]["index"]
    cap = caption(
        "primary, the held-out half of the source runs",
        set_tags(reg, src, f"pairs[index={first}].test_composition"),
    )
    header = [
        "Pair",
        "Stage 1",
        "Stage 2",
        "Escalation rate",
        "Cascade recall (test)",
        "Stage 2 alone (test)",
        "Cost, s per trace (test)",
        "Cost cap",
        "H4.1",
        "H4.2",
    ]
    return framed(
        f"E4 cascades on held-out data, estimate [{p('95%')} interval]", cap, md_table(header, rows)
    )


# ---------------------------------------------------------------- corpus

CORPUS_ROWS: tuple[tuple[str, str, Callable[[dict[str, Any]], bool]], ...] = (
    (
        "real_benign",
        "Real agent runs, benign",
        lambda t: t["source"] == "real_agent" and t["label"] == "benign",
    ),
    (
        "natural_violation",
        "Real agent runs, natural violation",
        lambda t: t["source"] == "real_agent" and t["label"] == "violation",
    ),
    (
        "inserted_violation",
        "Inserted violation, same agent continues",
        lambda t: t["source"] == "inserted",
    ),
    (
        "procedural_benign",
        "Procedural variant of a real benign run",
        lambda t: t["source"] == "procedural",
    ),
    (
        "dataset_benign",
        "Armour dataset, benign",
        lambda t: t["source"] == "dataset" and t["label"] == "benign",
    ),
    (
        "dataset_violation",
        "Armour dataset, violation",
        lambda t: t["source"] == "dataset" and t["label"] == "violation",
    ),
)
TRACE_FIELDS = (
    "trace_id",
    "source",
    "label",
    "synthetic",
    "source_run_id",
    "agent_id",
    "domain",
    "violation_type",
    "violation_source",
)


def iter_corpus(manifest: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """The fields of every corpus trace that the tables need, shard by shard."""
    for shard in manifest["shards"]:
        with gzip.open(ROOT / shard["path"], "rt", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    t = json.loads(line)
                    yield {k: t.get(k) for k in TRACE_FIELDS}


def stats(traces: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Size, synthetic share, and independent source runs of a group of traces."""
    n = len(traces)
    return {
        "traces": n,
        "synthetic_share": sum(bool(t["synthetic"]) for t in traces) / n if n else 0.0,
        "independent_source_runs": len({t["source_run_id"] for t in traces}),
    }


def corpus_numbers(manifest: dict[str, Any], audit: dict[str, Any] | None) -> dict[str, Any]:
    """Counts the corpus tables and prose cite, computed from the banked shards."""
    traces = list(iter_corpus(manifest))
    rows = []
    for key, label, pred in CORPUS_ROWS:
        group = [t for t in traces if pred(t)]
        if group:
            rows.append({"key": key, "label": label, **stats(group)})
    bad = [t for t in traces if t["label"] == "violation"]
    types = []
    for vt in sorted({str(t["violation_type"]) for t in bad}):
        row: dict[str, Any] = {"type": vt}
        for vs in ("natural", "inserted", "dataset"):
            row[vs] = sum(t["violation_type"] == vt and t["violation_source"] == vs for t in bad)
        row["total"] = sum(t["violation_type"] == vt for t in bad)
        types.append(row)
    subject = [t for t in traces if t["source"] in {"real_agent", "inserted"}]
    agents = []
    for agent in sorted({t["agent_id"] for t in subject}):
        mine = [t for t in subject if t["agent_id"] == agent]
        agents.append(
            {
                "agent": agent,
                "real_benign": sum(
                    t["source"] == "real_agent" and t["label"] == "benign" for t in mine
                ),
                "natural_violation": sum(
                    t["source"] == "real_agent" and t["label"] == "violation" for t in mine
                ),
                "inserted_violation": sum(t["source"] == "inserted" for t in mine),
                "independent_source_runs": len({t["source_run_id"] for t in mine}),
            }
        )
    relabel = manifest.get("relabeling", {})
    changes = []
    for key, count in sorted(relabel.get("label_changes", {}).items()):
        before, _, after = key.partition(" -> ")
        changes.append({"from": before, "to": after, "count": int(count)})
    out: dict[str, Any] = {
        "rows": rows,
        "total": {
            **stats(traces),
            "benign": sum(t["label"] == "benign" for t in traces),
            "violations": len(bad),
        },
        "violations": stats(bad),
        "types": types,
        "agents": agents,
        "real_and_inserted": stats(subject),
        "relabeling": {
            "replayed": int(relabel.get("replayed", 0)),
            "inserted_dropped": int(relabel.get("inserted_dropped", 0)),
            "changed": sum(c["count"] for c in changes),
            "changes": changes,
        },
    }
    out["seeds"] = list(manifest.get("task_seeds", []))
    insertion = manifest.get("insertion")
    if insertion:
        out["insertion"] = {
            "attempts": insertion["attempts"],
            "inserted": insertion["inserted"],
            "skipped": sum(insertion["skipped"].values()),
            "by_type": [
                {
                    "type": r["type"],
                    "attempts": r["attempts"],
                    "inserted": r["inserted"],
                    "no_valid_call": r.get("no valid call of the drawn type", 0),
                    "no_usable_summary": r.get("no usable summary", 0),
                }
                for r in insertion["by_type"]
            ],
        }
    sources = manifest.get("armour", {}).get("label_sources", {})
    out["armour_label_sources"] = [{"source": k, "records": v} for k, v in sorted(sources.items())]
    out["armour"] = stats([t for t in traces if t["source"] == "dataset"])
    if audit is not None:
        by_id = {t["trace_id"]: t for t in traces}
        missing = sorted(i["trace_id"] for i in audit["items"] if i["trace_id"] not in by_id)
        if missing:
            raise SystemExit(
                f"label audit items are not in the corpus ({len(missing)}, e.g. {missing[0]}); "
                "audit the current corpus"
            )
        out["audit"] = stats([by_id[i["trace_id"]] for i in audit["items"]])
    return out


def table_corpus(reg: Registry, nums: dict[str, Any]) -> str:
    """Corpus composition by source."""
    rows = []
    for r in nums["corpus"]["rows"]:
        base = f"corpus.rows[key={r['key']}]"
        cid = f"corpus.{r['key']}"
        rows.append(
            [
                r["label"],
                reg.tag(f"{cid}.traces", NUMBERS, f"{base}.traces", "{:,d}"),
                reg.tag(f"{cid}.runs", NUMBERS, f"{base}.independent_source_runs", "{:,d}"),
                reg.tag(f"{cid}.synthetic", NUMBERS, f"{base}.synthetic_share", "{:.3f}"),
            ]
        )
    rows.append(
        [
            "All traces",
            reg.tag("corpus.total.traces", NUMBERS, "corpus.total.traces", "{:,d}"),
            reg.tag("corpus.total.runs", NUMBERS, "corpus.total.independent_source_runs", "{:,d}"),
            reg.tag("corpus.total.synthetic", NUMBERS, "corpus.total.synthetic_share", "{:.3f}"),
        ]
    )
    cap = caption("full corpus", set_tags(reg, NUMBERS, "corpus.total"))
    header = ["Source", "Traces", "Independent source runs", "Synthetic share"]
    return framed("Corpus composition by source", cap, md_table(header, rows))


def table_types(reg: Registry, nums: dict[str, Any]) -> str:
    """Violations by type and source."""
    rows = []
    for r in nums["corpus"]["types"]:
        base = f"corpus.types[type={r['type']}]"
        cells = [name(r["type"])]
        for col in ("natural", "inserted", "dataset", "total"):
            cells.append(
                reg.tag(f"corpus.type.{r['type']}.{col}", NUMBERS, f"{base}.{col}", "{:,d}")
            )
        rows.append(cells)
    cap = caption("violations in the full corpus", set_tags(reg, NUMBERS, "corpus.violations"))
    header = ["Violation type", "Natural", "Inserted", "Armour dataset", "All"]
    return framed("Violations by type and source", cap, md_table(header, rows))


def table_agents(reg: Registry, nums: dict[str, Any]) -> str:
    """Real-agent and inserted traces by subject model."""
    rows = []
    for r in nums["corpus"]["agents"]:
        base = f"corpus.agents[agent={r['agent']}]"
        cid = cid_part(f"corpus.agent.{r['agent']}")
        cells = [name(r["agent"])]
        for col in (
            "real_benign",
            "natural_violation",
            "inserted_violation",
            "independent_source_runs",
        ):
            cells.append(reg.tag(f"{cid}.{col}", NUMBERS, f"{base}.{col}", "{:,d}"))
        rows.append(cells)
    cap = caption(
        "real agent runs and inserted violations",
        set_tags(reg, NUMBERS, "corpus.real_and_inserted"),
    )
    header = [
        "Subject model",
        "Benign runs",
        "Natural violations",
        "Inserted violations",
        "Independent source runs",
    ]
    return framed("Traces by subject model", cap, md_table(header, rows))


def table_relabel(reg: Registry, nums: dict[str, Any]) -> str:
    """Label changes from the oracle amendment (prereg/0006)."""
    rel = nums["corpus"]["relabeling"]
    rows = []
    for c in rel["changes"]:
        sel = f"corpus.relabeling.changes[from={c['from']},to={c['to']}].count"
        cid = cid_part(f"corpus.relabel.{c['from']}.{c['to']}")
        after = name(c["to"]) + (" (first violating step changed)" if c["to"] == c["from"] else "")
        rows.append([name(c["from"]), after, reg.tag(cid, NUMBERS, sel, "{:,d}")])
    replayed = reg.tag("corpus.relabel.replayed", NUMBERS, "corpus.relabeling.replayed", "{:,d}")
    dropped = reg.tag(
        "corpus.relabel.dropped", NUMBERS, "corpus.relabeling.inserted_dropped", "{:,d}"
    )
    changed = reg.tag("corpus.relabel.changed", NUMBERS, "corpus.relabeling.changed", "{:,d}")
    if not rows:
        rows.append(["(none)", "(none)", changed])
    cap = caption(
        "real agent runs and inserted violations",
        set_tags(reg, NUMBERS, "corpus.real_and_inserted"),
    )
    title = (
        f"Labels changed by relabeling under the fixed oracle (prereg/0006): {changed} of "
        f"{replayed} replayed traces; inserted traces dropped: {dropped}"
    )
    return framed(title, cap, md_table(["Generation label", "Label after replay", "Traces"], rows))


def table_insertion(reg: Registry, nums: dict[str, Any]) -> str:
    """Controlled insertion: attempts, inserted traces, and skips by type (prereg/0007)."""
    rows = []
    for r in nums["corpus"]["insertion"]["by_type"]:
        base = f"corpus.insertion.by_type[type={r['type']}]"
        cells = [name(r["type"])]
        for col in ("attempts", "inserted", "no_valid_call", "no_usable_summary"):
            cells.append(
                reg.tag(f"corpus.ins.{r['type']}.{col}", NUMBERS, f"{base}.{col}", "{:,d}")
            )
        rows.append(cells)
    rows.append(
        [
            "All",
            reg.tag("corpus.ins.attempts", NUMBERS, "corpus.insertion.attempts", "{:,d}"),
            reg.tag("corpus.ins.inserted", NUMBERS, "corpus.insertion.inserted", "{:,d}"),
            "",
            "",
        ]
    )
    cap = caption(
        "real agent runs and inserted violations",
        set_tags(reg, NUMBERS, "corpus.real_and_inserted"),
    )
    header = [
        "Violation type",
        "Attempts",
        "Inserted",
        "Skipped: no valid call",
        "Skipped: no usable summary",
    ]
    return framed("Controlled insertion attempts (prereg/0007)", cap, md_table(header, rows))


def table_armour_labels(reg: Registry, nums: dict[str, Any]) -> str:
    """Where Armour's labels come from, as Armour records them."""
    rows = [
        [
            name(r["source"]),
            reg.tag(
                cid_part(f"corpus.armour.{r['source']}"),
                NUMBERS,
                f"corpus.armour_label_sources[source={r['source']}].records",
                "{:,d}",
            ),
        ]
        for r in nums["corpus"]["armour_label_sources"]
    ]
    cap = caption("Armour dataset records", set_tags(reg, NUMBERS, "corpus.armour"))
    return framed(
        "Label sources of Armour's records", cap, md_table(["Label source", "Records"], rows)
    )


def table_audit(reg: Registry, audit: dict[str, Any]) -> str:
    """Blind label audit agreement by stratum."""
    src = "artifacts/corpus/label_audit.json"
    rows = []
    for stratum in sorted(audit["by_stratum"]):
        base = f'by_stratum["{stratum}"]'
        cid = cid_part(f"audit.{stratum}")
        rows.append(
            [
                stratum,
                reg.tag(f"{cid}.n", src, f"{base}.n", "{:,d}"),
                reg.tag(f"{cid}.agreement", src, f"{base}.agreement", "{:.3f}"),
                reg.tag(f"{cid}.label", src, f"{base}.label_agreement", "{:.3f}"),
            ]
        )
    rows.append(
        [
            "All",
            reg.tag("audit.n", src, "sample_size", "{:,d}"),
            reg.tag("audit.agreement", src, "agreement", "{:.3f}")
            + " ["
            + reg.tag("audit.agreement.lo", src, "agreement_ci[0]", "{:.3f}")
            + ", "
            + reg.tag("audit.agreement.hi", src, "agreement_ci[1]", "{:.3f}")
            + "]",
            reg.tag("audit.label", src, "label_agreement", "{:.3f}")
            + " ["
            + reg.tag("audit.label.lo", src, "label_agreement_ci[0]", "{:.3f}")
            + ", "
            + reg.tag("audit.label.hi", src, "label_agreement_ci[1]", "{:.3f}")
            + "]",
        ]
    )
    cap = caption("label audit sample", set_tags(reg, NUMBERS, "corpus.audit"))
    header = ["Stratum", "Items", "Full agreement (label, type, step)", "Label agreement"]
    title = f"Blind label audit, agreement [{p('95%')} interval]"
    return framed(title, cap, md_table(header, rows))


def write_table(name_: str, rendered: str) -> str:
    """Bank a rendered table; return its path."""
    TAB.mkdir(parents=True, exist_ok=True)
    path = TAB / f"{name_}.md"
    path.write_text(rendered + "\n")
    return str(path.relative_to(ROOT))


# ---------------------------------------------------------------- documents


CITE_RE = re.compile(r"\[@([A-Za-z0-9][\w\-]*)\]")


def display_title(ref: dict[str, Any]) -> str:
    """The title the citation check saw, without site decorations.

    ``page_title`` is what the check read (arXiv ids, Crossref notes, and site
    names around it are removed); ``claimed`` is what the brief asserted and may
    be a short form. The claimed spelling is used only when it equals the
    verified title apart from letter case and punctuation (Crossref registers
    some titles in capitals).
    """
    seen = str(ref.get("page_title") or "")
    title = re.sub(r"^\[\d{4}\.\d{4,5}\]\s+", "", seen)
    title = re.sub(r"\s+\(Crossref title[^)]*\)$", "", title)
    title = re.sub(r"\s+\\\s+Anthropic$", "", title)
    title = re.sub(r"\s+-\s+by\s+.+$", "", title)
    title = re.sub(r"^GitHub - ", "", title).rstrip(" .")
    claimed = str(ref.get("claimed", {}).get("title") or "")

    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", text.lower())

    if claimed and norm(claimed) == norm(title):
        return claimed
    return title or claimed or str(ref["key"])


def references(keys: Sequence[str]) -> str:
    """Reference list (template syntax) for cited keys, from artifacts/citecheck.json.

    Each entry shows the title the citation check verified (:func:`display_title`),
    the year, and the DOI, arXiv, or publisher link the reference names (not the
    API address the check may have read). Authors are not shown: the check's
    record holds them only as free text.
    """
    data = load("artifacts/citecheck.json")
    entries = {r["key"]: r for r in data["references"]}
    lines = []
    for key in keys:
        ref = entries.get(key)
        if ref is None or not ref.get("verified"):
            raise SystemExit(f"cited reference {key} is missing or unverified in citecheck.json")
        claimed = ref.get("claimed", {})
        year = claimed.get("year")
        url = claimed.get("url") or ref["final_url"]
        when = f" {{{{date:{year}}}}}." if year else ""
        lines.append(f"- [@{key}] {_tag_digits(display_title(ref))}.{when} <{url}>")
    return "\n".join(lines)


def _tag_digits(text: str) -> str:
    """Wrap digit-bearing words of a title in label allowances."""
    return " ".join(f"{{{{label:{w}}}}}" if re.search(r"\d", w) else w for w in text.split())


def build_templates(fragments: dict[str, str]) -> list[str]:
    """Expand ``<!-- @table NAME -->`` markers in the prose sources into templates.

    Citations ``[@key]`` whose key holds a digit (a year) are wrapped in name
    allowances, and a
    ``<!-- @references -->`` marker becomes the reference list of every key the
    document cites.
    """
    docs = []
    for doc, prose, tmpl in (
        ("README.md", "README.prose.md", "README.md.tmpl"),
        ("docs/REPORT.md", "REPORT.prose.md", "REPORT.md.tmpl"),
        ("docs/DATA.md", "DATA.prose.md", "DATA.md.tmpl"),
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
        text = CITE_RE.sub(
            lambda m: f"{{{{name:[@{m.group(1)}]}}}}"
            if re.search(r"\d", m.group(1))
            else m.group(0),
            text,
        )
        (TEMPLATES / tmpl).write_text(text)
        docs.append(doc)
    return docs


def main(argv: Sequence[str] | None = None) -> int:
    """Build everything; render the documents."""
    del argv
    style()
    reg = Registry()
    figures: list[str] = []
    fragments: dict[str, str] = {}

    def has(rel: str) -> bool:
        return (ROOT / rel).exists()

    manifest = (
        load("artifacts/corpus/manifest.json") if has("artifacts/corpus/manifest.json") else None
    )
    audit = (
        load("artifacts/corpus/label_audit.json")
        if has("artifacts/corpus/label_audit.json")
        else None
    )
    # every output is rebuilt, so an output that a build no longer makes shows up as a
    # deletion in git instead of lingering with old numbers
    for directory in (FIG, TAB):
        for stale in directory.glob("*") if directory.is_dir() else ():
            if stale.is_file():
                stale.unlink()
    read = (
        "artifacts/corpus/manifest.json",
        "artifacts/corpus/label_audit.json",
        "artifacts/e0/summary.json",
        E2_SRC,
        "artifacts/e3/results.json",
        "artifacts/e4/results.json",
        "artifacts/citecheck.json",
    )
    numbers: dict[str, Any] = {
        "schema": "baserate-report-numbers/1",
        "sources": {rel: sha(ROOT / rel) for rel in read if has(rel)},
    }
    if manifest is not None:
        numbers["corpus"] = corpus_numbers(manifest, audit)
    OUT.mkdir(parents=True, exist_ok=True)
    (ROOT / NUMBERS).write_text(json.dumps(numbers, indent=1, sort_keys=True) + "\n")

    e0 = load("artifacts/e0/summary.json")
    figures.append(fig_e0(e0))
    if manifest is not None:
        fragments["corpus"] = table_corpus(reg, numbers)
        fragments["corpus-types"] = table_types(reg, numbers)
        fragments["corpus-agents"] = table_agents(reg, numbers)
        fragments["corpus-relabel"] = table_relabel(reg, numbers)
        if "insertion" in numbers["corpus"]:
            fragments["corpus-insertion"] = table_insertion(reg, numbers)
        if numbers["corpus"]["armour_label_sources"]:
            fragments["corpus-armour-labels"] = table_armour_labels(reg, numbers)
        if audit is not None:
            fragments["audit"] = table_audit(reg, audit)
    if has(E2_SRC):
        e2 = load(E2_SRC)
        figures.append(fig_budget_panels(e2, "recall_at_budget", "headline_recall", log=False))
        figures.append(fig_budget_panels(e2, "precision_at_budget", "precision", log=True))
        figures.append(fig_ladder(e2))
        for metric in ("recall_at_budget", "precision_at_budget"):
            fragments[f"e2-{metric}"] = table_e2(reg, e2, metric)
        if any(e["set"] == "dataset" for e in e2["estimates"]):
            fragments["e2-dataset"] = table_e2(reg, e2, "recall_at_budget", "dataset")
        if any(e["set"] == "full" for e in e2["estimates"]):
            fragments["e2-full"] = table_e2(reg, e2, "recall_at_budget", "full")
        fragments["readme-headline"] = table_headline(reg, e2)
        for key, builder in (
            ("e2-fixed", table_fixed),
            ("e2-ttd", table_ttd),
            ("e2-breakdown", table_breakdown),
            ("e2-ladder", table_ladder),
        ):
            fragment = builder(reg, e2)
            if fragment:
                fragments[key] = fragment
    if has("artifacts/e3/results.json"):
        e3 = load("artifacts/e3/results.json")
        figures.append(fig_transfer(e3))
        fragments["e3"] = table_e3(reg, e3)
        fragments["e3-pairs"] = table_e3_pairs(reg, e3)
    if has("artifacts/e4/results.json"):
        e4 = load("artifacts/e4/results.json")
        figures.append(fig_cascades(e4))
        fragments["e4"] = table_e4(reg, e4)
    tables = [write_table(k, render_fragment(reg, v)) for k, v in sorted(fragments.items())]
    manifest_out = {
        "figures": figures,
        "tables": tables,
        "numbers": NUMBERS,
        "hashes": {f: sha(ROOT / f) for f in [*figures, *tables, NUMBERS]},
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest_out, indent=1, sort_keys=True) + "\n")
    reg.add_prose_claims(TEMPLATES / "claims.prose.json")
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
