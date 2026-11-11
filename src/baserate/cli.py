"""Command-line entry point for ``baserate``.

Commands:

- ``eval``: recall, precision, and FPR at review budgets and prevalences, with
  intervals, from a score file.
- ``certify``: independent runs needed to certify a false-positive rate (and
  recall), and how many more runs an existing claim needs.
- ``cascade``: two-stage cascade sweep and cost-recall Pareto front.
- ``card``: a monitor report card in Markdown or JSON.
- ``demo``: the offline demo on recorded fixtures.
- ``budget``: usage caps and cash spend.

Score files are described in :mod:`baserate.scores` and docs/USAGE.md.
Exit codes: 0 on success, 2 on a usage error (bad or missing arguments), 1 on
a data error (a missing or invalid score file, or data a computation cannot
use).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from baserate import __version__, budget
from baserate.card import (
    CLUSTER_AWARE_METHODS,
    budget_table,
    build_card,
    confidence_text,
    e0_cell_text,
    e0_support_source,
    fmt_interval,
    fmt_prob,
    fmt_small,
    jsonable,
    method_note,
    render_json,
    render_markdown,
)
from baserate.cascade import CascadePoint, default_escalation_rates, sweep_cascade
from baserate.certify import (
    JointPlan,
    MoreRuns,
    RunPlan,
    fpr_runs_for_power,
    joint_plan,
    more_runs_fpr,
    more_runs_recall,
    recall_runs_for_power,
    zero_fp_runs,
    zero_miss_runs,
)
from baserate.certify.samplesize import DEFAULT_N_MAX
from baserate.estimate import BUDGET_METHODS, flag_rate_from_budget
from baserate.estimate.intervals import zero_event_upper
from baserate.scores import FORMATS, ScoreFileError, ScoreSet, read_scores

EXIT_OK = 0
EXIT_DATA = 1
EXIT_USAGE = 2

Handler = Callable[[argparse.Namespace, argparse.ArgumentParser], int]


# ------------------------------------------------------------ argument types


def _float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"must be finite, got {text!r}")
    return value


def _open_unit(text: str) -> float:
    """A float strictly between 0 and 1."""
    value = _float(text)
    if not 0.0 < value < 1.0:
        raise argparse.ArgumentTypeError(f"must be between 0 and 1 (exclusive), got {text}")
    return value


def _unit(text: str) -> float:
    """A float in [0, 1]."""
    value = _float(text)
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(f"must be between 0 and 1, got {text}")
    return value


def _budget(text: str) -> float:
    """Reviews per 10,000 trajectories, in (0, 10,000]."""
    value = _float(text)
    if not 0.0 < value <= 10_000:
        raise argparse.ArgumentTypeError(
            f"a budget is reviews per 10,000 trajectories, in (0, 10000]; got {text}"
        )
    return value


def _cost(text: str) -> float:
    value = _float(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be non-negative, got {text}")
    return value


def _count(minimum: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}, got {text}")
        return value

    return parse


# ------------------------------------------------------------------- helpers


def _load(ns: argparse.Namespace) -> ScoreSet:
    return read_scores(ns.scores, ns.format)


def _emit(text: str) -> None:
    sys.stdout.write(text if text.endswith("\n") else text + "\n")


def _dump(obj: Any) -> None:
    _emit(json.dumps(jsonable(obj), indent=2, allow_nan=False))


def _table(header: Sequence[str], rows: Sequence[Sequence[str]], indent: str = "") -> list[str]:
    """Left-aligned columns padded to their widest cell."""
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]

    def line(cells: Sequence[str]) -> str:
        return indent + "  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip()

    return [line(header), *(line(r) for r in rows)]


def _sizes(scores: ScoreSet) -> str:
    return (
        f"benign {scores.n_neg:,} traces in {scores.n_neg_clusters:,} independent clusters; "
        f"violation {scores.n_pos:,} traces in {scores.n_pos_clusters:,} independent clusters"
    )


# ---------------------------------------------------------------------- eval


def _cmd_eval(ns: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """``baserate eval``: the budget table for the requested prevalences and budgets."""
    scores = _load(ns)
    rows = budget_table(scores, ns.prevalence, ns.budget, ns.method, ns.alpha, ns.reps, ns.seed)
    conf = confidence_text(ns.alpha)
    floor = float(zero_event_upper(scores.n_neg_clusters, ns.alpha))
    if ns.json:
        _dump(
            {
                "schema": "baserate-eval/1",
                "baserate_version": __version__,
                "source": scores.source,
                "sha256": scores.sha256,
                "method": ns.method,
                "cluster_aware": ns.method in CLUSTER_AWARE_METHODS,
                "alpha": ns.alpha,
                "reps": ns.reps,
                "seed": ns.seed,
                "weighted": scores.weights is not None,
                "n_neg": scores.n_neg,
                "n_pos": scores.n_pos,
                "n_neg_clusters": scores.n_neg_clusters,
                "n_pos_clusters": scores.n_pos_clusters,
                "fpr_floor_one_sided": floor,
                "e0_support_source": e0_support_source(),
                "rows": [r.to_dict() for r in rows],
            }
        )
        return EXIT_OK
    lines = [
        f"baserate eval: {scores.source}",
        _sizes(scores),
        f"intervals: {ns.method}, {conf} two-sided (resamples {ns.reps:,}, seed {ns.seed}); "
        f"{method_note(ns.method)}"
        + ("; sampling weights used" if scores.weights is not None else ""),
        f"with {scores.n_neg_clusters:,} independent benign clusters no false-positive rate "
        f"below {fmt_small(floor)} can be certified at {conf}",
        f"E0 support data: {e0_support_source()}",
        "",
    ]
    header = [
        "prevalence",
        "budget/10k",
        f"recall [{conf}]",
        f"precision [{conf}]",
        f"FPR [{conf}]",
        "benign (clusters)",
        "violation (clusters)",
    ]
    cells = [
        [
            f"{r.prevalence:g}",
            f"{r.budget_per_10k:g}",
            fmt_interval(r.recall),
            fmt_interval(r.precision),
            fmt_interval(r.fpr, small=True),
            f"{r.n_neg:,} ({r.n_neg_clusters:,})",
            f"{r.n_pos:,} ({r.n_pos_clusters:,})",
        ]
        for r in rows
    ]
    table = _table(header, cells)
    lines.append(table[0])
    for r, text in zip(rows, table[1:], strict=True):
        warn = f"  WARNING: {e0_cell_text(r.warnings, ns.method)}" if r.warnings else ""
        lines.append(text + warn)
    _emit("\n".join(lines))
    return EXIT_OK


# ------------------------------------------------------------------- certify


def _plan_text(plan: RunPlan, unit: str, event: str) -> str:
    assumed = "a true FPR" if plan.metric == "fpr" else "a true recall"
    return (
        f"for power {plan.power_target:g} assuming {assumed} of {plan.assumed:g}: "
        f"{plan.runs:,} independent {unit} (certifies with up to {plan.events_allowed:,} "
        f"{event}; power {fmt_prob(plan.power or 0.0)} there and at least "
        f"{fmt_prob(plan.min_power or 0.0)} up to {plan.horizon:,} runs)"
    )


def _more_text(more: MoreRuns, unit: str, event: str, events: str, claim: str) -> list[str]:
    observed = f"{more.events:,} of {more.runs:,} {unit} with {event}"
    side = "upper" if more.metric == "fpr" else "lower"
    out = [
        f"  existing data: {observed}; one-sided {side} bound {fmt_small(more.current_bound)}",
    ]
    if more.certified:
        out.append(f"  already certifies {claim}")
        return out
    no_event = event.removeprefix("a ")
    out.append(f"  {more.if_no_events:,} more {unit} with no {no_event} needed to certify {claim}")
    if more.at_observed_rate is not None:
        out.append(f"  {more.at_observed_rate:,} more if {events} continue at the observed rate")
    else:
        out.append(f"  at the observed rate no number of added runs certifies {claim}")
    if more.for_power is not None and more.power_target is not None:
        out.append(
            f"  {more.for_power:,} more for power {more.power_target:g} at the observed rate"
        )
    return out


def _safe_plan(make: Callable[[], RunPlan]) -> RunPlan | str:
    try:
        return make()
    except ValueError as exc:
        return str(exc)


def _cmd_certify(ns: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """``baserate certify``: run counts for the claims and the gap of existing claims."""
    f, r, alpha, power = ns.fpr, ns.recall, ns.alpha, ns.power
    if ns.have_events is not None and ns.have_runs is None:
        parser.error("--have-events needs --have-runs")
    if ns.have_runs is not None and (ns.have_events or 0) > ns.have_runs:
        parser.error("--have-events cannot exceed --have-runs")
    if (ns.have_misses is not None or ns.have_violation_runs is not None) and r is None:
        parser.error("--have-violation-runs and --have-misses need --recall")
    if ns.have_misses is not None and ns.have_violation_runs is None:
        parser.error("--have-misses needs --have-violation-runs")
    if ns.have_violation_runs is not None and (ns.have_misses or 0) > ns.have_violation_runs:
        parser.error("--have-misses cannot exceed --have-violation-runs")
    true_fpr = f / 2 if ns.true_fpr is None else ns.true_fpr
    if not 0.0 <= true_fpr < f:
        parser.error(f"--true-fpr must be at least 0 and below --fpr ({f:g})")
    if ns.true_recall is not None and r is None:
        parser.error("--true-recall needs --recall")
    true_recall = None if r is None else ((1 + r) / 2 if ns.true_recall is None else ns.true_recall)
    if r is not None and true_recall is not None and not r < true_recall <= 1.0:
        parser.error(f"--true-recall must be above --recall ({r:g}) and at most 1")

    conf = confidence_text(alpha)
    fpr_claim = f"FPR <= {f:g} at {conf}"
    fpr_zero = zero_fp_runs(f, alpha)
    fpr_power = _safe_plan(lambda: fpr_runs_for_power(f, true_fpr, power, alpha, ns.n_max))
    rec_zero: int | None = None
    rec_power: RunPlan | str | None = None
    joint: JointPlan | None = None
    joint_zero: JointPlan | None = None
    if r is not None and true_recall is not None:
        rec_zero = zero_miss_runs(r, alpha)
        tr = true_recall
        rec_power = _safe_plan(lambda: recall_runs_for_power(r, tr, power, alpha, ns.n_max))
        joint_zero = joint_plan(r, f, alpha)
        try:
            joint = joint_plan(
                r, f, alpha, true_recall=tr, true_fpr=true_fpr, power=power, n_max=ns.n_max
            )
        except ValueError:
            joint = None
    more_fpr = (
        None
        if ns.have_runs is None
        else more_runs_fpr(ns.have_events or 0, ns.have_runs, f, alpha, power, ns.n_max)
    )
    more_rec = None
    if ns.have_violation_runs is not None and r is not None:
        detected = ns.have_violation_runs - (ns.have_misses or 0)
        more_rec = more_runs_recall(detected, ns.have_violation_runs, r, alpha, power, ns.n_max)

    if ns.json:

        def plan_json(p: RunPlan | str | None) -> Any:
            return p if p is None or isinstance(p, str) else asdict(p)

        _dump(
            {
                "schema": "baserate-certify/1",
                "baserate_version": __version__,
                "alpha": alpha,
                "confidence": 1 - alpha,
                "power": power,
                "fpr": {
                    "max_fpr": f,
                    "zero_event_runs": fpr_zero,
                    "assumed_true_fpr": true_fpr,
                    "power_plan": plan_json(fpr_power),
                    "existing": None if more_fpr is None else asdict(more_fpr),
                },
                "recall": None
                if r is None
                else {
                    "min_recall": r,
                    "zero_event_runs": rec_zero,
                    "assumed_true_recall": true_recall,
                    "power_plan": plan_json(rec_power),
                    "existing": None if more_rec is None else asdict(more_rec),
                },
                "joint_zero_event": None if joint_zero is None else asdict(joint_zero),
                "joint_power": None if joint is None else asdict(joint),
            }
        )
        return EXIT_OK

    fpr_power_text = (
        fpr_power
        if isinstance(fpr_power, str)
        else _plan_text(fpr_power, "benign runs", "false positives")
    )
    lines = [
        f"baserate certify: one-sided {conf} claims; every count is of independent source runs",
        "",
        f"claim FPR <= {f:g}:",
        f"  zero-event: {fpr_zero:,} independent benign runs, none with a false positive "
        "(closed form ceil(ln(alpha) / ln(1 - F)); about 3 / F at 95%)",
        f"  {fpr_power_text}",
    ]
    if more_fpr is not None:
        lines += _more_text(
            more_fpr, "benign runs", "a false positive", "false positives", fpr_claim
        )
    if r is not None and rec_zero is not None and rec_power is not None:
        rec_claim = f"recall >= {r:g} at {conf}"
        rec_power_text = (
            rec_power
            if isinstance(rec_power, str)
            else _plan_text(rec_power, "violation runs", "misses")
        )
        lines += [
            "",
            f"claim recall >= {r:g}:",
            f"  zero-event: {rec_zero:,} independent violation runs, all detected "
            "(closed form ceil(ln(alpha) / ln(R)))",
            f"  {rec_power_text}",
        ]
        if more_rec is not None:
            lines += _more_text(more_rec, "violation runs", "a miss", "misses", rec_claim)
        if joint_zero is not None:
            lines += [
                "",
                f"both claims at an overall {conf} (alpha split {joint_zero.split}: "
                f"{joint_zero.alpha_recall:g} recall, {joint_zero.alpha_fpr:g} FPR):",
                f"  zero-event: {joint_zero.n_neg:,} benign runs and {joint_zero.n_pos:,} "
                "violation runs",
            ]
            if joint is not None and joint.joint_power is not None:
                lines.append(
                    f"  for joint power {power:g}: {joint.n_neg:,} benign runs and "
                    f"{joint.n_pos:,} violation runs (joint power {fmt_prob(joint.joint_power)})"
                )
            else:
                lines.append(f"  joint power {power:g} is not reached within {ns.n_max:,} runs")
    lines += [
        "",
        "Counts are of independent source runs. With several traces per run, a benign run is a "
        "false positive if any of its traces is flagged, and a violation run is detected only "
        "if all of its traces are flagged.",
    ]
    _emit("\n".join(lines))
    return EXIT_OK


# ------------------------------------------------------------------- cascade


def _point_cells(p: CascadePoint) -> list[str]:
    return [
        p.kind,
        f"{p.escalation_rate:.4g}",
        f"{p.cost:.4g}",
        fmt_prob(p.recall),
        fmt_prob(p.precision),
        fmt_small(p.fpr),
        fmt_small(p.review_rate),
    ]


def _point_json(p: CascadePoint) -> dict[str, Any]:
    return asdict(p)


def _cmd_cascade(ns: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """``baserate cascade``: the two-stage sweep, its Pareto front, and the best point."""
    scores = _load(ns)
    if scores.weights is not None:
        raise ScoreFileError(
            f"{scores.source}: the cascade sweep does not take sampling weights; "
            "remove the weight column"
        )
    flag_rate = flag_rate_from_budget(ns.budget)
    rates = default_escalation_rates(flag_rate, ns.num_rates)
    sweep = sweep_cascade(scores.two_stage(), ns.prevalence, flag_rate, ns.c1, ns.c2, rates)
    best = None if ns.max_cost is None else sweep.best_under(ns.max_cost)
    s2 = scores.stage2_scores
    n_s2 = 0 if s2 is None else int(np.sum(~np.isnan(s2)))
    if ns.json:
        _dump(
            {
                "schema": "baserate-cascade/1",
                "baserate_version": __version__,
                "source": scores.source,
                "sha256": scores.sha256,
                "prevalence": ns.prevalence,
                "budget_per_10k": ns.budget,
                "flag_rate": flag_rate,
                "c1": ns.c1,
                "c2": ns.c2,
                "max_cost": ns.max_cost,
                "n_neg": scores.n_neg,
                "n_pos": scores.n_pos,
                "n_neg_clusters": scores.n_neg_clusters,
                "n_pos_clusters": scores.n_pos_clusters,
                "n_stage2_scored": n_s2,
                "in_sample": True,
                "front": [_point_json(p) for p in sweep.front],
                "candidates": [_point_json(p) for p in sweep.candidates],
                "stage1": _point_json(sweep.stage1),
                "stage2": None if sweep.stage2 is None else _point_json(sweep.stage2),
                "best_under_max_cost": None if best is None else _point_json(best),
            }
        )
        return EXIT_OK
    header = ["kind", "escalation", "cost", "recall", "precision", "FPR", "review rate"]
    lines = [
        f"baserate cascade: {scores.source}",
        _sizes(scores),
        f"stage 1: score column; stage 2: stage2_score column ({n_s2:,} of "
        f"{scores.n_traces:,} traces scored)",
        f"prevalence {ns.prevalence:g}; review budget {ns.budget:g} per 10,000 "
        f"(flag rate {flag_rate:g}); cost per trajectory {ns.c1:g} for stage 1 plus "
        f"{ns.c2:g} per escalated trajectory",
        "point estimates on these traces, without intervals; a rule picked here is picked on "
        "the same data, so check it on held-out runs (baserate.cascade.validate_holdout)",
        "",
        "Pareto front (no other point costs less and recalls more), by cost:",
        *_table(header, [_point_cells(p) for p in sweep.front], indent="  "),
    ]
    if ns.all:
        lines += [
            "",
            f"all {len(sweep.candidates)} cascades on the escalation grid, then the baselines:",
            *_table(header, [_point_cells(p) for p in sweep.points], indent="  "),
        ]
    if ns.max_cost is not None:
        lines.append("")
        if best is None:
            lines.append(f"no cascade or single monitor costs at most {ns.max_cost:g}")
        else:
            what = {
                "cascade": f"cascade escalating {best.escalation_rate:.4g} of traffic",
                "stage1": "stage 1 alone",
                "stage2": "stage 2 alone on all traffic",
            }[best.kind]
            lines.append(
                f"best under cost {ns.max_cost:g}: {what}: cost {best.cost:.4g}, recall "
                f"{fmt_prob(best.recall)}, precision {fmt_prob(best.precision)}"
            )
    _emit("\n".join(lines))
    return EXIT_OK


# ---------------------------------------------------------------------- card


def _cmd_card(ns: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """``baserate card``: the report card, printed or written to ``--out``."""
    if not ns.name.strip():
        parser.error("--name must not be empty")
    scores = _load(ns)
    card = build_card(
        scores,
        ns.name,
        threshold=ns.threshold,
        method=ns.method,
        alpha=ns.alpha,
        reps=ns.reps,
        seed=ns.seed,
        notes=ns.note or (),
    )
    text = render_json(card) if ns.json else render_markdown(card)
    if ns.out is None:
        _emit(text)
        return EXIT_OK
    out = Path(ns.out)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ScoreFileError(f"cannot write {out}: {exc.strerror}") from None
    _emit(f"baserate card: wrote {out}")
    return EXIT_OK


# -------------------------------------------------------------------- parser


def _run_demo(argv: Sequence[str]) -> int:
    from baserate import demo

    return demo.main(list(argv))


DELEGATES: dict[str, Callable[[Sequence[str]], int]] = {
    "budget": budget.main,
    "demo": _run_demo,
}
"""Commands whose own parser handles every argument after the command name."""

HANDLERS: dict[str, Handler] = {
    "eval": _cmd_eval,
    "certify": _cmd_certify,
    "cascade": _cmd_cascade,
    "card": _cmd_card,
}


def _add_scores(p: argparse.ArgumentParser) -> None:
    p.add_argument("scores", metavar="SCORES", help="score file (.csv or .jsonl; docs/USAGE.md)")
    p.add_argument(
        "--format", choices=FORMATS, help="score file format (default: from the file suffix)"
    )


def _add_intervals(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--method",
        choices=BUDGET_METHODS,
        default="composite",
        metavar="METHOD",
        help="interval method for recall at a budget, one of "
        f"{', '.join(BUDGET_METHODS)} (default: composite)",
    )
    p.add_argument(
        "--alpha",
        type=_open_unit,
        default=0.05,
        help="miss probability of each two-sided interval (default: 0.05, for 95%%)",
    )
    p.add_argument(
        "--reps",
        type=_count(1),
        default=1000,
        help="resamples for the resampling interval methods (default: 1000)",
    )
    p.add_argument("--seed", type=_count(0), default=0, help="resampling seed (default: 0)")


def _subparsers() -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
    parser = argparse.ArgumentParser(
        prog="baserate",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"baserate {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    subs: dict[str, argparse.ArgumentParser] = {}

    p = sub.add_parser(
        "eval",
        help="recall, precision, and FPR at review budgets, with intervals",
        description="Recall, precision, and false-positive rate when the top-scoring share of "
        "traffic is reviewed, at each review budget and prevalence, with intervals that count "
        "independent clusters (baserate.estimate.recall_at_budget).",
    )
    _add_scores(p)
    p.add_argument(
        "--prevalence",
        type=_open_unit,
        nargs="+",
        required=True,
        metavar="P",
        help="deployment prevalence(s) of violations, for example 1e-3",
    )
    p.add_argument(
        "--budget",
        type=_budget,
        nargs="+",
        required=True,
        metavar="B",
        help="review budget(s) per 10,000 trajectories, for example 20",
    )
    _add_intervals(p)
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    subs["eval"] = p

    p = sub.add_parser(
        "certify",
        help="independent runs needed to certify an FPR (and recall)",
        description="Independent runs needed to certify FPR <= F (and recall >= R) with "
        "one-sided exact bounds: the zero-event closed form, the runs needed for a target "
        "power under an assumed true rate, and how many more runs an existing claim needs "
        "(baserate.certify).",
    )
    p.add_argument("--fpr", type=_open_unit, required=True, metavar="F", help="FPR to certify")
    p.add_argument("--recall", type=_open_unit, metavar="R", help="recall to certify")
    p.add_argument(
        "--alpha",
        type=_open_unit,
        default=0.05,
        help="miss probability of each one-sided bound (default: 0.05, for 95%%)",
    )
    p.add_argument(
        "--power",
        type=_open_unit,
        default=0.8,
        help="target probability of certifying, for the power-based counts (default: 0.8)",
    )
    p.add_argument(
        "--true-fpr",
        type=_unit,
        metavar="F0",
        help="assumed true FPR for the power-based count (default: F / 2)",
    )
    p.add_argument(
        "--true-recall",
        type=_unit,
        metavar="R0",
        help="assumed true recall for the power-based count (default: (1 + R) / 2)",
    )
    p.add_argument(
        "--have-runs",
        type=_count(1),
        metavar="N",
        help="independent benign runs you already have",
    )
    p.add_argument(
        "--have-events",
        type=_count(0),
        metavar="K",
        help="how many of them had a false positive (default: 0)",
    )
    p.add_argument(
        "--have-violation-runs",
        type=_count(1),
        metavar="N",
        help="independent violation runs you already have (with --recall)",
    )
    p.add_argument(
        "--have-misses",
        type=_count(0),
        metavar="K",
        help="how many of them were missed (default: 0)",
    )
    p.add_argument(
        "--n-max",
        type=_count(1),
        default=DEFAULT_N_MAX,
        help=f"largest number of runs searched (default: {DEFAULT_N_MAX:,})",
    )
    p.add_argument("--json", action="store_true", help="print JSON")
    subs["certify"] = p

    p = sub.add_parser(
        "cascade",
        help="two-stage cascade sweep and cost-recall Pareto front",
        description="Sweep two-stage cascades (stage 1 scores everything, stage 2 scores the "
        "escalated share) at a review budget and print the cost-recall Pareto front "
        "(baserate.cascade). SCORES needs score and stage2_score columns.",
    )
    _add_scores(p)
    p.add_argument("--prevalence", type=_open_unit, required=True, metavar="P")
    p.add_argument("--budget", type=_budget, required=True, metavar="B", help="reviews per 10,000")
    p.add_argument("--c1", type=_cost, required=True, help="stage-1 cost per trajectory")
    p.add_argument("--c2", type=_cost, required=True, help="stage-2 cost per escalated trajectory")
    p.add_argument(
        "--max-cost", type=_cost, metavar="C", help="report the best point costing at most C"
    )
    p.add_argument(
        "--num-rates",
        type=_count(1),
        default=25,
        help="escalation rates on the geometric grid from the flag rate to 1 (default: 25)",
    )
    p.add_argument("--all", action="store_true", help="also print every swept cascade")
    p.add_argument("--json", action="store_true", help="print JSON")
    subs["cascade"] = p

    p = sub.add_parser(
        "card",
        help="monitor report card (Markdown or JSON)",
        description="A monitor report card: data summary, recall and precision at budgets "
        "of 5, 20, and 100 per 10,000 and prevalences of 1e-2, 1e-3, and 1e-4, the "
        "false-positive rate at the decision threshold with cluster-aware bounds, and the "
        "certification gap in plain words (baserate.card).",
    )
    _add_scores(p)
    p.add_argument("--name", required=True, help="the monitor's name, shown in the title")
    p.add_argument(
        "--threshold",
        type=_float,
        metavar="T",
        help="the monitor's own decision threshold (flag when score >= T); without it the "
        "decision-threshold section is left out",
    )
    p.add_argument(
        "--note",
        action="append",
        metavar="TEXT",
        help="a line for the Notes section, for example where the data came from (repeatable)",
    )
    _add_intervals(p)
    p.add_argument("--json", action="store_true", help="JSON instead of Markdown")
    p.add_argument("--out", metavar="PATH", help="write the card here instead of printing it")
    subs["card"] = p

    sub.add_parser("demo", help="offline demo on recorded fixtures", add_help=False)
    sub.add_parser("budget", help="check usage caps and cash spend", add_help=False)
    return parser, subs


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    return _subparsers()[0]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code (0 ok, 1 data error, 2 usage error).

    Usage errors exit through argparse (``SystemExit`` with code 2).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    parser, subs = _subparsers()
    known, rest = parser.parse_known_args(args)
    command: str = known.command
    if command in DELEGATES:
        return DELEGATES[command](rest)
    sub = subs[command]
    if rest:
        sub.error(f"unrecognized arguments: {' '.join(rest)}")
    try:
        return HANDLERS[command](known, sub)
    except ValueError as exc:  # ScoreFileError is a ValueError
        sys.stderr.write(f"baserate {command}: error: {exc}\n")
        return EXIT_DATA


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
