"""Analyses E2, E3, and E4 from banked scores (no model calls).

    python tools/analyze.py e2 prereg/0003-e2-main-study.md
    python tools/analyze.py e3 prereg/0004-e3-transfer.md
    python tools/analyze.py e4 prereg/0005-e4-cascades.md

Each command reads its preregistration's frozen config, the corpus shards
(labels, clusters, sources), and the score files banked by
tools/score_monitors.py under artifacts/scores/, and writes
``artifacts/<e2|e3|e4>/results.json`` with ``confirmatory: true``, the
preregistration link and config hash, every estimate with its interval, and
provenance. E2 also bundles the scores it used into artifacts/e2/scores.jsonl.gz.
Seeds come from the configs, so reruns reproduce every number exactly.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from baserate.cascade import (
    escalation_rule,
    split_by_run,
    sweep_cascade,
    two_stage_scores,
    validate_holdout,
)
from baserate.cascade.ensemble import ensemble_scores
from baserate.estimate import (
    budget_point,
    fixed_threshold,
    flag_rate_from_budget,
    recall_at_budget,
)
from baserate.estimate.bootstrap import cluster_index, cluster_weights, percentile_interval
from baserate.traces.schema import Trace

REPO = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("BASERATE_ROOT", REPO)).resolve()
"""Data root; tests point it at a temporary tree."""
sys.path.insert(0, str(REPO / "tools"))

from score_monitors import corpus_traces, in_set  # noqa: E402

FloatArray = NDArray[np.float64]


def canonical_sha256(obj: Any) -> str:
    """SHA-256 of canonical JSON."""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def prereg_config(path: Path) -> dict[str, Any]:
    """The frozen config block of a preregistration."""
    blocks = re.findall(r"```json prereg-config\n(.*?)```", path.read_text(), re.S)
    if len(blocks) != 1:
        raise SystemExit(f"{path} needs exactly one prereg-config block")
    cfg: dict[str, Any] = json.loads(blocks[0])
    return cfg


def stable_seed(base: int, *parts: object) -> int:
    """A reproducible per-estimate seed."""
    h = hashlib.sha256(":".join(map(str, (base, *parts))).encode()).hexdigest()
    return int(h[:8], 16)


def git_sha() -> str:
    """Current commit."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()


def safe(monitor: str) -> str:
    """File-system name of a monitor id."""
    return monitor.replace(":", "_")


def load_scores(set_name: str, monitor: str) -> dict[str, dict[str, Any]]:
    """Banked score records of a base monitor on a set, by trace id."""
    path = ROOT / "artifacts" / "scores" / set_name / f"{safe(monitor)}.jsonl.gz"
    if not path.exists():
        raise SystemExit(f"missing score file {path.relative_to(ROOT)}")
    out = {}
    for line in gzip.decompress(path.read_bytes()).decode().splitlines():
        if line.strip():
            rec = json.loads(line)
            out[rec["trace_id"]] = rec
    return out


def by_score(recs: dict[str, dict[str, Any]]) -> Callable[[Trace], float]:
    """Score lookup from banked records."""
    return lambda t: float(recs[t.trace_id]["score"])


def by_value(scores: dict[str, float]) -> Callable[[Trace], float]:
    """Score lookup from a computed score map."""
    return lambda t: scores[t.trace_id]


@dataclass
class Split:
    """Scores of one monitor on one set, split by class."""

    neg: FloatArray
    pos: FloatArray
    neg_clusters: NDArray[Any]
    pos_clusters: NDArray[Any]
    neg_traces: list[Trace]
    pos_traces: list[Trace]


def split(traces: Sequence[Trace], score_of: Callable[[Trace], float]) -> Split:
    """Class arrays with source-run clusters, in trace order."""
    neg_t = [t for t in traces if t.label == "benign"]
    pos_t = [t for t in traces if t.label == "violation"]
    return Split(
        neg=np.array([score_of(t) for t in neg_t], dtype=np.float64),
        pos=np.array([score_of(t) for t in pos_t], dtype=np.float64),
        neg_clusters=np.array([t.source_run_id for t in neg_t]),
        pos_clusters=np.array([t.source_run_id for t in pos_t]),
        neg_traces=neg_t,
        pos_traces=pos_t,
    )


def composition(traces: Sequence[Trace]) -> dict[str, Any]:
    """Size, synthetic share, and independent source runs of a set."""
    n = len(traces)
    return {
        "traces": n,
        "benign": sum(t.label == "benign" for t in traces),
        "violations": sum(t.label == "violation" for t in traces),
        "synthetic_share": (sum(t.synthetic for t in traces) / n) if n else 0.0,
        "independent_source_runs": len({t.source_run_id for t in traces}),
    }


# ----------------------------------------------------------------------- E0 link


def e0_lookup() -> tuple[dict[str, bool], dict[str, Any]]:
    """E0 cell pass flags by id, and the E0 config (designs, distributions)."""
    summary = json.loads((ROOT / "artifacts" / "e0" / "summary.json").read_text())
    config = prereg_config(ROOT / summary["prereg"])
    return {c["id"]: bool(c["passes"]) for c in summary["cells"]}, config


def relied_cells(
    e0cfg: dict[str, Any],
    method: str,
    prevalence: float,
    budget: float,
    n_neg: int,
    n_pos: int,
    clustered: bool,
) -> list[str]:
    """The E0 cells an estimate relies on (frozen rule of prereg 0001)."""
    pool = [
        (k, d) for k, d in e0cfg["designs"].items() if (int(d["neg_cluster_size"]) > 1) == clustered
    ]

    def dist(d: dict[str, Any]) -> float:
        nn = int(d["neg_clusters"]) * int(d["neg_cluster_size"])
        return abs(math.log(nn) - math.log(n_neg)) + abs(
            math.log(int(d["n_pos"])) - math.log(n_pos)
        )

    design = min(pool, key=lambda item: (dist(item[1]), item[0]))[0]
    return [
        f"budget_recall:{method}|{s}|{design}|p{prevalence:g}|b{budget:g}"
        for s in e0cfg["grid"]["score_distributions"]
    ]


# ---------------------------------------------------------------------------- E2


def derived_scores(
    cfg: dict[str, Any],
    set_traces: Sequence[Trace],
    set_name: str,
    monitor: dict[str, Any],
    prevalence: float,
) -> dict[str, float]:
    """Scores of an ensemble or cascade monitor, computed from its members' banked scores."""
    if monitor["family"] == "ensemble":
        members = [load_scores(set_name, m) for m in monitor["members"]]
        neg_t = [t for t in set_traces if t.label == "benign"]
        pos_t = [t for t in set_traces if t.label == "violation"]
        neg = [np.array([s[t.trace_id]["score"] for t in neg_t]) for s in members]
        pos = [np.array([s[t.trace_id]["score"] for t in pos_t]) for s in members]
        en, ep = ensemble_scores(neg, pos, how=monitor["how"], reference="benign")
        out = {t.trace_id: float(v) for t, v in zip(neg_t, en, strict=True)}
        out.update({t.trace_id: float(v) for t, v in zip(pos_t, ep, strict=True)})
        return out
    s1 = load_scores(set_name, monitor["stage1"])
    s2 = load_scores(set_name, monitor["stage2"])
    sp = split(set_traces, by_score(s1))
    sp2 = split(set_traces, by_score(s2))
    ts = two_stage_scores(sp.neg, sp2.neg, sp.pos, sp2.pos)
    tau, _ = escalation_rule(ts, prevalence, float(monitor["escalation_rate"]))
    # composite score: escalated traces rank by stage 2 above every non-escalated trace
    # (ties at tau escalate; documented in docs/REPORT.md)
    out = {}
    for t in set_traces:
        a, b = s1[t.trace_id]["score"], s2[t.trace_id]["score"]
        out[t.trace_id] = 1.0 + b if a >= tau else 0.5 * a
    return out


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    """The lower weighted median, or NaN when no value has positive weight."""
    keep = weights > 0
    v, w = values[keep], weights[keep]
    if v.size == 0:
        return math.nan
    order = np.argsort(v, kind="stable")
    v, w = v[order], w[order]
    cum = np.cumsum(w)
    # a relative tolerance keeps float rounding at the half-mass point from picking
    # the upper median (for example 0.3 + 0.7 summing to just under 1)
    return float(v[int(np.searchsorted(cum, 0.5 * cum[-1] * (1 - 1e-9)))])


def ttd(
    prefix_recs: Sequence[dict[str, Any]],
    threshold: float,
    tie_fraction: float,
    reps: int,
    seed: int,
) -> dict[str, Any]:
    """Time to detection at the budget threshold (prereg/0003).

    A violation is caught at the first prefix that the budget reviews. At the
    randomized budget threshold that is the first prefix scoring above it, or,
    with probability equal to the threshold's tie fraction (one draw per
    trace), the first prefix scoring at or above it. The shares and the median
    delay are expected values over that draw, so a monitor whose scores tie at
    the threshold gets only the tie fraction's credit for them. Violations no
    prefix of which is reviewed count as missed.
    """
    ge: list[float] = []
    gt: list[float] = []
    for rec in prefix_recs:
        scores = rec.get("prefix_scores", [])
        ge.append(next((float(i) for i, s in enumerate(scores) if s >= threshold), math.inf))
        gt.append(next((float(i) for i, s in enumerate(scores) if s > threshold), math.inf))
    a, b = np.array(ge), np.array(gt)
    t = float(tie_fraction)
    out: dict[str, Any] = {"violations": int(a.size), "tie_fraction": t}
    if a.size == 0:
        return out

    def summary(rows: np.ndarray) -> tuple[float, float, float]:
        x, y = a[rows], b[rows]
        fx, fy = np.isfinite(x), np.isfinite(y)
        now = t * float((x == 0).mean()) + (1 - t) * float((y == 0).mean())
        caught = t * float(fx.mean()) + (1 - t) * float(fy.mean())
        values = np.concatenate([x[fx], y[fy]])
        weights = np.concatenate([np.full(int(fx.sum()), t), np.full(int(fy.sum()), 1 - t)])
        return now, caught, _weighted_median(values, weights)

    now, caught, med = summary(np.arange(a.size))
    rng = np.random.default_rng(seed)
    boots = [summary(row) for row in rng.integers(0, a.size, size=(reps, a.size))]
    out["share_detected_at_violating_step"] = now
    out["share_detected_at_violating_step_ci"] = list(
        percentile_interval(np.array([s[0] for s in boots]))
    )
    out["share_caught"] = caught
    if not math.isnan(med):
        meds = np.array([s[2] for s in boots if not math.isnan(s[2])])
        out["median_delay_steps"] = med
        out["median_delay_steps_ci"] = list(percentile_interval(meds)) if meds.size else None
    return out


def run_e2(prereg: Path) -> dict[str, Any]:
    """The main study."""
    cfg = prereg_config(prereg)
    head = cfg["headline"]
    method = head["method"]
    if method == "TO_BE_SET_FROM_E0":
        raise SystemExit("the E2 headline method is not set")
    passes, e0cfg = e0_lookup()
    manifest = ROOT / "artifacts" / "corpus" / "manifest.json"
    corpus = list(corpus_traces(manifest))
    sets = {s: [t for t in corpus if in_set(t, s)] for s in cfg["sets"]}
    estimates: list[dict[str, Any]] = []
    fixed_rows: list[dict[str, Any]] = []
    ttd_rows: list[dict[str, Any]] = []
    breakdown: list[dict[str, Any]] = []
    bundle: list[dict[str, Any]] = []
    for mon in cfg["monitors"]:
        for set_name in mon["sets"]:
            traces = sets[set_name]
            base = mon["family"] in {"rule", "model"}
            recs = load_scores(set_name, mon["id"]) if base else None
            for prev in cfg["prevalences"]:
                scores = (
                    {k: float(v["score"]) for k, v in recs.items()}
                    if recs is not None
                    else derived_scores(cfg, traces, set_name, mon, float(prev))
                )
                sp = split(traces, by_value(scores))
                clustered = len(set(sp.neg_clusters.tolist())) < sp.neg.size
                for budget in cfg["budgets_per_10k"]:
                    q = flag_rate_from_budget(float(budget))
                    seed = stable_seed(head["seed"], mon["id"], set_name, prev, budget)
                    est = recall_at_budget(
                        sp.neg,
                        sp.pos,
                        float(prev),
                        q,
                        method,
                        neg_clusters=sp.neg_clusters,
                        pos_clusters=sp.pos_clusters,
                        reps=int(head["resamples"]),
                        rng=np.random.default_rng(seed),
                        warn=False,
                    )
                    cells = relied_cells(
                        e0cfg, method, float(prev), float(budget), est.n_neg, est.n_pos, clustered
                    )
                    ok = all(passes.get(c, False) for c in cells)
                    headline = set_name == head["set"] and ok
                    common = {
                        "family": mon["family"],
                        "monitor": mon["id"],
                        "set": set_name,
                        "prevalence": float(prev),
                        "budget": int(budget),
                        "method": method,
                        "alpha": head["alpha"],
                        "n_neg": est.n_neg,
                        "n_pos": est.n_pos,
                        "n_neg_clusters": est.n_neg_clusters,
                        "n_pos_clusters": est.n_pos_clusters,
                        "n_independent_runs": est.n_neg_clusters + est.n_pos_clusters,
                        "clustered": clustered,
                        "headline": headline,
                        "e0_cells": cells,
                        "e0_supported": ok,
                        "threshold": est.point.threshold,
                    }
                    for metric, iv in (
                        ("recall_at_budget", est.recall),
                        ("precision_at_budget", est.precision),
                        ("fpr_at_budget", est.fpr),
                    ):
                        estimates.append(
                            {
                                **common,
                                "metric": metric,
                                "estimate": iv.estimate,
                                "ci_low": iv.lower,
                                "ci_high": iv.upper,
                            }
                        )
                if mon["id"] in cfg["fixed_threshold"]["monitors"]:
                    ft = cfg["fixed_threshold"]
                    fx = fixed_threshold(
                        sp.neg,
                        sp.pos,
                        float(ft["threshold"]),
                        float(prev),
                        ft["method"],
                        neg_clusters=sp.neg_clusters,
                        pos_clusters=sp.pos_clusters,
                    )
                    for metric, iv in (
                        ("recall", fx.recall),
                        ("fpr", fx.fpr),
                        ("precision", fx.precision),
                    ):
                        fixed_rows.append(
                            {
                                "monitor": mon["id"],
                                "set": set_name,
                                "prevalence": float(prev),
                                "threshold": ft["threshold"],
                                "method": ft["method"],
                                "metric": metric,
                                "estimate": iv.estimate,
                                "ci_low": iv.lower,
                                "ci_high": iv.upper,
                                "n_neg_clusters": fx.n_neg_clusters,
                                "n_pos_clusters": fx.n_pos_clusters,
                            }
                        )
            if set_name == "primary" and recs is not None:
                for t in traces:
                    bundle.append(
                        {
                            "monitor": mon["id"],
                            "set": set_name,
                            "trace_id": t.trace_id,
                            "label": t.label,
                            "source_run_id": t.source_run_id,
                            "score": float(recs[t.trace_id]["score"]),
                        }
                    )
    # time to detection
    tcfg = cfg["time_to_detection"]
    primary = sets["primary"]
    for mid in tcfg["monitors"]:
        recs = load_scores("primary", mid)
        sp = split(primary, by_score(recs))
        op = budget_point(
            sp.neg,
            sp.pos,
            float(tcfg["prevalence"]),
            flag_rate_from_budget(float(tcfg["budget_per_10k"])),
        )
        prefix = [recs[t.trace_id] for t in sp.pos_traces if "prefix_scores" in recs[t.trace_id]]
        ttd_rows.append(
            {
                "monitor": mid,
                "prevalence": tcfg["prevalence"],
                "budget": tcfg["budget_per_10k"],
                "threshold": op.threshold,
                **ttd(
                    prefix,
                    op.threshold,
                    op.tie_fraction,
                    2000,
                    stable_seed(head["seed"], "ttd", mid),
                ),
            }
        )
    # breakdowns on the primary set at the size-ladder setting
    lad = cfg["size_ladder"]
    lp, lb = float(lad["prevalence"]), float(lad["budget_per_10k"])
    for mon in cfg["monitors"]:
        if mon["family"] not in {"rule", "model"} or "primary" not in mon["sets"]:
            continue
        recs = load_scores("primary", mon["id"])
        sp = split(primary, by_score(recs))
        groups: dict[str, Callable[[Trace], str]] = {
            "violation_source": lambda t: str(t.violation_source),
            "violation_type": lambda t: str(t.violation_type),
            "subject_model": lambda t: t.agent_id,
        }
        for gname, key in groups.items():
            for value in sorted({key(t) for t in sp.pos_traces}):
                mask = np.array([key(t) == value for t in sp.pos_traces])
                if mask.sum() < 1:
                    continue
                est = recall_at_budget(
                    sp.neg,
                    sp.pos[mask],
                    lp,
                    flag_rate_from_budget(lb),
                    method,
                    neg_clusters=sp.neg_clusters,
                    pos_clusters=sp.pos_clusters[mask],
                    reps=int(head["resamples"]),
                    rng=np.random.default_rng(stable_seed(head["seed"], mon["id"], gname, value)),
                    warn=False,
                )
                breakdown.append(
                    {
                        "monitor": mon["id"],
                        "by": gname,
                        "value": value,
                        "n_pos": int(mask.sum()),
                        "prevalence": lp,
                        "budget": int(lb),
                        "recall": est.recall.estimate,
                        "ci_low": est.recall.lower,
                        "ci_high": est.recall.upper,
                    }
                )

    # size ladder
    def recall_at(mid: str) -> dict[str, Any]:
        e = next(
            x
            for x in estimates
            if x["monitor"] == mid
            and x["set"] == "primary"
            and x["metric"] == "recall_at_budget"
            and x["prevalence"] == lp
            and x["budget"] == int(lb)
        )
        return e

    rungs = sorted(
        (m for m in cfg["monitors"] if m["family"] == "model"), key=lambda m: float(m["params_b"])
    )
    rules = [m["id"] for m in cfg["monitors"] if m["family"] == "rule"]
    best_rule = max(rules, key=lambda r: recall_at(r)["estimate"])
    br = recall_at(best_rule)["estimate"]
    crossing = next((m["id"] for m in rungs if recall_at(m["id"])["estimate"] > br), None)
    ladder = {
        "prevalence": lp,
        "budget": int(lb),
        "best_rule_monitor": best_rule,
        "best_rule_recall": br,
        "rungs": [
            {
                "monitor": m["id"],
                "params_b": m["params_b"],
                "recall": recall_at(m["id"])["estimate"],
                "ci_low": recall_at(m["id"])["ci_low"],
                "ci_high": recall_at(m["id"])["ci_high"],
            }
            for m in rungs
        ],
        "crossing_rung": crossing,
        "crossing_is_clear": bool(crossing is not None and recall_at(crossing)["ci_low"] > br),
    }
    raw = (
        "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in bundle) + "\n"
    ).encode()
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as gz:
        gz.write(raw)
    scores_path = ROOT / "artifacts" / "e2" / "scores.jsonl.gz"
    scores_path.parent.mkdir(parents=True, exist_ok=True)
    scores_path.write_bytes(buf.getvalue())
    return {
        "experiment": "E2",
        "confirmatory": True,
        "prereg": str(prereg.relative_to(ROOT)),
        "prereg_config_sha256": canonical_sha256(cfg),
        "headline_method": method,
        "sets": {s: composition(v) for s, v in sets.items()},
        "estimates": estimates,
        "fixed_threshold": fixed_rows,
        "time_to_detection": ttd_rows,
        "breakdowns": breakdown,
        "size_ladder": ladder,
        "scores_file": str(scores_path.relative_to(ROOT)),
        "scores_sha256": hashlib.sha256(buf.getvalue()).hexdigest(),
        "scores_records_sha256": hashlib.sha256(raw).hexdigest(),
        "corpus_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "provenance": {
            "git_sha": git_sha(),
            "command": " ".join(sys.argv),
            "analyzed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "model_calls": 0,
        },
    }


# ---------------------------------------------------------------------------- E3


def _batch_review(
    neg: FloatArray,
    pos: FloatArray,
    w_neg: FloatArray,
    w_pos: FloatArray,
    thr: FloatArray,
    tie: FloatArray,
    prevalence: float,
) -> tuple[FloatArray, FloatArray]:
    """Realized review rate and recall at given thresholds under resampling weights."""

    def tail(scores: FloatArray, w: FloatArray) -> FloatArray:
        above = (scores[None, :] > thr[:, None]).astype(float)
        at = (scores[None, :] == thr[:, None]).astype(float)
        mass = (above + tie[:, None] * at) * w
        return np.asarray(mass.sum(axis=1) / w.sum(axis=1), dtype=np.float64)

    fpr = tail(neg, w_neg)
    rec = tail(pos, w_pos)
    return (1 - prevalence) * fpr + prevalence * rec, rec


def _weights(clusters: NDArray[Any], reps: int, rng: np.random.Generator) -> FloatArray:
    return cluster_weights(cluster_index(clusters, clusters.size), reps, rng, "multinomial")


def run_e3(prereg: Path) -> dict[str, Any]:
    """Threshold transfer across domains and prevalences."""
    from baserate.estimate.operating import budget_recall_batch

    cfg = prereg_config(prereg)
    manifest = ROOT / "artifacts" / "corpus" / "manifest.json"
    traces = [t for t in corpus_traces(manifest) if in_set(t, cfg["set"])]
    # prereg/0004 (D22): every benign trace of the full set, Armour's included, and the
    # natural and inserted violations; Armour's violations are left out
    traces = [t for t in traces if not (t.label == "violation" and t.source == "dataset")]
    prev, q = float(cfg["prevalence"]), flag_rate_from_budget(float(cfg["budget_per_10k"]))
    reps, band, floor = int(cfg["resamples"]), cfg["ratio_band"], float(cfg["recall_gap_floor"])
    domains = sorted({t.domain for t in traces})
    pairs_out: list[dict[str, Any]] = []
    prev_out: list[dict[str, Any]] = []
    hypotheses: list[dict[str, Any]] = []
    for mid in cfg["monitors"]:
        recs = load_scores(cfg["set"], mid)
        by_dom = {
            d: split(
                [t for t in traces if t.domain == d],
                by_score(recs),
            )
            for d in domains
        }
        rng = np.random.default_rng(stable_seed(cfg["seed"], mid))
        for a in domains:
            sa = by_dom[a]
            wn_a, wp_a = _weights(sa.neg_clusters, reps, rng), _weights(sa.pos_clusters, reps, rng)
            _, thr_b, tie_b = budget_recall_batch(sa.neg, sa.pos, prev, q, wn_a, wp_a)
            pt_a = budget_point(sa.neg, sa.pos, prev, q)
            for b in domains:
                if a == b:
                    continue
                sb = by_dom[b]
                wn_b, wp_b = (
                    _weights(sb.neg_clusters, reps, rng),
                    _weights(sb.pos_clusters, reps, rng),
                )
                rate, rec = _batch_review(sb.neg, sb.pos, wn_b, wp_b, thr_b, tie_b, prev)
                own, _, _ = budget_recall_batch(sb.neg, sb.pos, prev, q, wn_b, wp_b)
                one = np.ones(1)
                rate0, rec0 = _batch_review(
                    sb.neg,
                    sb.pos,
                    np.ones((1, sb.neg.size)),
                    np.ones((1, sb.pos.size)),
                    np.array([pt_a.threshold]),
                    np.array([pt_a.tie_fraction]),
                    prev,
                )
                own0 = budget_point(sb.neg, sb.pos, prev, q).recall
                ratio, gap = float(rate0[0] / q), float(rec0[0] - own0)
                pairs_out.append(
                    {
                        "monitor": mid,
                        "source": a,
                        "target": b,
                        "prevalence": prev,
                        "budget": cfg["budget_per_10k"],
                        "ratio": ratio,
                        "ratio_ci": list(percentile_interval(rate / q)),
                        "recall_gap": gap,
                        "recall_gap_ci": list(percentile_interval(rec - own)),
                        "n_source_runs": len(set(sa.neg_clusters.tolist())),
                        "n_target_runs": len(set(sb.neg_clusters.tolist())),
                    }
                )
                del one
        mine = [p for p in pairs_out if p["monitor"] == mid]
        share_ratio = sum(band[0] <= p["ratio"] <= band[1] for p in mine) / len(mine)
        share_gap = sum(p["recall_gap"] >= floor for p in mine) / len(mine)

        def outcome(share: float) -> str:
            if share >= cfg["pair_share_supported"]:
                return "supported"
            if share < cfg["pair_share_not_supported"]:
                return "not_supported"
            return "inconclusive"

        short = mid.split(":", 1)[1]
        hypotheses.append(
            {
                "id": f"H3.1-{short}",
                "monitor": mid,
                "share": share_ratio,
                "outcome": outcome(share_ratio),
            }
        )
        hypotheses.append(
            {
                "id": f"H3.2-{short}",
                "monitor": mid,
                "share": share_gap,
                "outcome": outcome(share_gap),
            }
        )
        # prevalence transfer
        allsp = split(traces, by_score(recs))
        src_prev = float(cfg["source_prevalence"])
        wn, wp = _weights(allsp.neg_clusters, reps, rng), _weights(allsp.pos_clusters, reps, rng)
        _, thr_s, tie_s = budget_recall_batch(allsp.neg, allsp.pos, src_prev, q, wn, wp)
        pt_s = budget_point(allsp.neg, allsp.pos, src_prev, q)
        for tgt in cfg["target_prevalences"]:
            rate, _ = _batch_review(allsp.neg, allsp.pos, wn, wp, thr_s, tie_s, float(tgt))
            rate0, _ = _batch_review(
                allsp.neg,
                allsp.pos,
                np.ones((1, allsp.neg.size)),
                np.ones((1, allsp.pos.size)),
                np.array([pt_s.threshold]),
                np.array([pt_s.tie_fraction]),
                float(tgt),
            )
            lo, hi = percentile_interval(rate / q, float(cfg["alpha"]))
            ratio = float(rate0[0] / q)
            lo, hi = min(lo, ratio), max(hi, ratio)
            if band[0] <= lo and hi <= band[1]:
                out = "supported"
            elif hi < band[0] or lo > band[1]:
                out = "not_supported"
            else:
                out = "inconclusive"
            hid = f"H3.3-{short}-p{float(tgt):g}"
            prev_out.append(
                {
                    "monitor": mid,
                    "source_prevalence": src_prev,
                    "target_prevalence": float(tgt),
                    "ratio": ratio,
                    "ratio_ci": [lo, hi],
                    "hypothesis": hid,
                }
            )
            hypotheses.append(
                {"id": hid, "monitor": mid, "outcome": out, "ratio": ratio, "ratio_ci": [lo, hi]}
            )
    return {
        "experiment": "E3",
        "confirmatory": True,
        "prereg": str(prereg.relative_to(ROOT)),
        "prereg_config_sha256": canonical_sha256(cfg),
        "set_composition": composition(traces),
        "domain_pairs": pairs_out,
        "prevalence_transfer": prev_out,
        "hypotheses": hypotheses,
        "provenance": {
            "git_sha": git_sha(),
            "command": " ".join(sys.argv),
            "analyzed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "model_calls": 0,
        },
    }


# ---------------------------------------------------------------------------- E4


def median_seconds(set_name: str, monitor: str) -> float:
    """Median recorded scoring time per trace (the E4 cost proxy)."""
    recs = load_scores(set_name, monitor)
    # a record served from the response cache took no model time, so it is left out
    secs = [
        float(r["seconds"])
        for r in recs.values()
        if r.get("seconds") is not None and not r.get("cached", False)
    ]
    if not secs:
        raise SystemExit(f"{monitor} has no recorded per-trace times outside the cache")
    return float(np.median(secs))


def run_e4(prereg: Path) -> dict[str, Any]:
    """Cascade selection on training data, validated on test data."""
    cfg = prereg_config(prereg)
    e2cfg = prereg_config(ROOT / cfg["scores_from"])
    method = e2cfg["headline"]["method"]
    manifest = ROOT / "artifacts" / "corpus" / "manifest.json"
    traces = [t for t in corpus_traces(manifest) if in_set(t, cfg["set"])]
    prev, q = float(cfg["prevalence"]), flag_rate_from_budget(float(cfg["budget_per_10k"]))
    pairs_out: list[dict[str, Any]] = []
    hypotheses: list[dict[str, Any]] = []
    for pair in cfg["pairs"]:
        r1, r2 = load_scores(cfg["set"], pair["stage1"]), load_scores(cfg["set"], pair["stage2"])
        s1 = split(traces, by_score(r1))
        s2 = split(traces, by_score(r2))
        ts = two_stage_scores(s1.neg, s2.neg, s1.pos, s2.pos, s1.neg_clusters, s1.pos_clusters)
        c1, c2 = (
            median_seconds(cfg["set"], pair["stage1"]),
            median_seconds(cfg["set"], pair["stage2"]),
        )
        max_cost = float(cfg["compute_budget_fraction_of_stage2"]) * c2
        train, test = split_by_run(ts, float(cfg["test_fraction"]), int(cfg["split_seed"]))
        assert test.neg_clusters is not None and test.pos_clusters is not None
        hold = validate_holdout(
            train,
            test,
            prev,
            q,
            c1,
            c2,
            max_cost,
            alpha=float(cfg["alpha"]),
            reps=int(cfg["resamples"]),
            seed=int(cfg["seed"]),
        )
        alone = recall_at_budget(
            test.s2_neg,
            test.s2_pos,
            prev,
            q,
            method,
            neg_clusters=test.neg_clusters,
            pos_clusters=test.pos_clusters,
            reps=int(cfg["resamples"]),
            rng=np.random.default_rng(stable_seed(cfg["seed"], pair["index"])),
            warn=False,
        )
        front = sweep_cascade(ts, prev, q, c1, c2).front
        train_recall = hold.chosen.recall
        h41 = (
            "supported"
            if hold.recall.lower <= train_recall <= hold.recall.upper
            else "not_supported"
        )
        margin = float(cfg["non_inferiority_margin"])
        target = alone.recall.estimate - margin
        if hold.recall.estimate >= target:
            h42 = "supported"
        elif hold.recall.upper < target:
            h42 = "not_supported"
        else:
            h42 = "inconclusive"
        pairs_out.append(
            {
                "index": pair["index"],
                "stage1": pair["stage1"],
                "stage2": pair["stage2"],
                "c1_seconds": c1,
                "c2_seconds": c2,
                "max_cost": max_cost,
                "chosen_escalation_rate": hold.chosen.escalation_rate,
                "train_recall": train_recall,
                "train_cost": hold.chosen.cost,
                "test_recall": hold.recall.estimate,
                "test_recall_ci": [hold.recall.lower, hold.recall.upper],
                "test_cost": hold.cost.estimate,
                "test_cost_ci": [hold.cost.lower, hold.cost.upper],
                "stage2_alone_test_recall": alone.recall.estimate,
                "stage2_alone_test_recall_ci": [alone.recall.lower, alone.recall.upper],
                "front": [
                    {
                        "kind": p.kind,
                        "escalation_rate": p.escalation_rate,
                        "cost": p.cost,
                        "recall": p.recall,
                        "precision": p.precision,
                    }
                    for p in front
                ],
                "n_train_runs": hold.n_train_neg_runs + hold.n_train_pos_runs,
                "n_test_runs": hold.n_test_neg_runs + hold.n_test_pos_runs,
                "test_composition": composition(
                    [
                        t
                        for t in traces
                        if t.source_run_id
                        in set(test.neg_clusters.tolist()) | set(test.pos_clusters.tolist())
                    ]
                ),
            }
        )
        hypotheses.append({"id": f"H4.1-{pair['index']}", "outcome": h41})
        hypotheses.append({"id": f"H4.2-{pair['index']}", "outcome": h42})
    return {
        "experiment": "E4",
        "confirmatory": True,
        "prereg": str(prereg.relative_to(ROOT)),
        "prereg_config_sha256": canonical_sha256(cfg),
        "set_composition": composition(traces),
        "pairs": pairs_out,
        "hypotheses": hypotheses,
        "provenance": {
            "git_sha": git_sha(),
            "command": " ".join(sys.argv),
            "analyzed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "model_calls": 0,
        },
    }


def write(name: str, result: dict[str, Any]) -> Path:
    """Bank a results artifact."""
    out = ROOT / "artifacts" / name / "results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    return out


def main(argv: Iterable[str] | None = None) -> int:
    """Command-line entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("experiment", choices=["e2", "e3", "e4"])
    ap.add_argument("prereg", type=Path)
    args = ap.parse_args(list(argv) if argv is not None else None)
    prereg = args.prereg.resolve()
    run = {"e2": run_e2, "e3": run_e3, "e4": run_e4}[args.experiment]
    path = write(args.experiment, run(prereg))
    print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
