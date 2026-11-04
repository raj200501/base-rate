"""Validate the certify calculators by simulation (RQ2).

    python tools/validate_certify.py OUT.json [--quick] [--seed 20261104]

Each row plans a study with a :mod:`baserate.certify` calculator, simulates
studies of the planned size, analyses every simulated study with the
library's own decision rule (the exact one-sided Clopper-Pearson bound of
:mod:`baserate.estimate.intervals`; :func:`baserate.estimate.recall_at_budget`
in budget mode), and compares the simulated confidence or power with its
target. A row meets its target when ``simulated >= target - 2 * mcse``, with
``mcse`` the Monte Carlo standard error of the simulated value. Where the
target is itself a simulation estimate (budget calibration rows), the
criterion uses the combined standard error of both. Where an exact value is
available it is recorded beside the simulated one. All data are synthetic:
binomial outcomes and binormal scores, no monitor or model results.

Checks:

- confidence: 1 minus the probability of certifying when the true rate
  equals the target (true FPR = max_fpr, or true recall = min_recall). This
  is the largest probability of certifying a false claim. Target 1 - alpha.
- simultaneous coverage (joint plans): probability that both one-sided
  bounds hold at once when both true rates equal their targets. Target
  1 - alpha.
- power: probability of certifying at the assumed true rate(s). Target: the
  requested power.

Grid (default settings; every alpha is 0.05 unless listed):

1. zero_fp_runs: max_fpr in {0.1, 0.01, 0.001} x alpha in {0.05, 0.01};
   confidence. Clustered check: max_fpr 0.01 with 3 traces per run (within-run
   correlated flags), analysed trace by trace with
   ``estimate.fixed.rate_interval(method="cluster_cp", alpha=2 * alpha)`` as
   ``certify.CLUSTER_NOTE`` prescribes.
2. zero_miss_runs: min_recall in {0.5, 0.8, 0.9, 0.95, 0.99} x alpha in
   {0.05, 0.01}; confidence. Clustered check: min_recall 0.9, 3 traces per run.
3. fpr_runs_for_power, (max_fpr, true_fpr, power): (0.01, 0.002, 0.8),
   (0.01, 0.005, 0.8), (0.05, 0.02, 0.9), (0.001, 0.0002, 0.8). Power at the
   planned n, at the n with the lowest exact power in [n, horizon] (the
   stays-above rule), and at the horizon; confidence at the planned n.
4. recall_runs_for_power, (min_recall, true_recall, power): (0.8, 0.9, 0.8),
   (0.9, 0.97, 0.8), (0.5, 0.7, 0.9), (0.95, 0.99, 0.8); the same four checks.
5. joint_plan: min_recall 0.8, max_fpr 0.01, true_recall 0.9, true_fpr 0.002,
   power 0.8, with the Bonferroni and the Sidak split (simultaneous coverage
   and joint power); min_recall 0.9, max_fpr 0.001 with no assumed rates
   (simultaneous coverage of the zero-event arms).
6. more_runs_fpr with power 0.8, observed (false positives, runs, max_fpr):
   (1, 300, 0.01), (4, 600, 0.01), (6, 200, 0.05). more_runs_recall with
   power 0.8, observed (detected, runs, min_recall): (95, 100, 0.9),
   (40, 45, 0.8), (18, 20, 0.75). Conditional power: the probability of
   certifying on all runs after ``for_power`` more runs whose events are
   Binomial at the observed rate.
7. Two looks, a diagnostic of a procedure the module does not guarantee (see
   "Adding runs after a look" in baserate.certify.samplesize): at the target
   rate, look at n0 runs and stop if the claim certifies; otherwise, unless
   the observed rate makes it unreachable, add ``for_power`` runs (power 0.8)
   and certify on all runs. FPR at max_fpr 0.01 with n0 in {100, 300, 1000};
   recall at min_recall 0.9 with n0 in {20, 50, 100}. Confidence as above.
8. Budget mode, also not guaranteed (a pilot-resampling estimate; see
   baserate.certify.atbudget). Synthetic binormal population (benign N(0, 1),
   violations N(3, 1)), prevalence 0.001, 20 reviews per 10,000. Independent
   pilots of 10,000 benign and 300 violation draws each give a plan_budget
   plan: min_recall 0.3, power 0.8, method composite, n_neg in {5000, 20000}
   x n_pos in {100, 300}. Per design, the planned power averaged over the
   pilots (target, with the pilot-to-pilot standard error of that mean as
   ``target_mcse``) is compared with the design's power over fresh studies
   drawn from the population (calibration). The designs the plans choose are
   compared, at their population power, with the 0.8 target.

Simulations (default; ``--quick`` in brackets): 100,000 [4,000] studies per
count-based row, 2,000 [300] per clustered row; budget mode 5 [2] pilots,
400 [40] planning studies per design and pilot, 2,000 [60] population
studies per design, and 200 [50] resamples per interval. ``--quick`` keeps
every grid point. With the defaults the run takes about 3 minutes on one
core (budget mode is nearly all of it: about 16,000 composite intervals);
``--quick`` takes under 10 seconds. Every row has its own seed, derived from
``--seed`` and the row id.

The output records, per row, the target, the simulated value, its MCSE, the
exact value where available, whether the simulated value meets the target
(``meets``), and whether the exact value does (``exact_meets``, free of
Monte Carlo error), plus provenance (git SHA, seeds, library versions,
command line). Rows whose exact value sits on the target (zero-event plans
are nearly exact) miss ``meets`` by noise alone up to about 2% of the time;
``exact_meets`` tells such a miss apart from a real one. The exit status is
1 when a row of a documented guarantee (``guaranteed: true``) misses
``meets``, else 0; the other rows are reported in full either way.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import numpy as np
import scipy
from numpy.typing import ArrayLike, NDArray
from scipy import stats

from baserate.certify import (
    BudgetPlan,
    RunPlan,
    fpr_power,
    fpr_runs_for_power,
    joint_plan,
    more_runs_fpr,
    more_runs_recall,
    plan_budget,
    recall_power,
    recall_runs_for_power,
    zero_fp_runs,
    zero_miss_runs,
)
from baserate.estimate import (
    flag_rate_from_budget,
    intervals,
    rate_interval,
    recall_at_budget,
    support,
)
from baserate.estimate.simulate import MODELS, truth

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEED = 20261104
ALPHA = 0.05

Metric = Literal["fpr", "recall"]
FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]
BoolArray = NDArray[np.bool_]
Row = dict[str, Any]

# ---------------------------------------------------------------- grid

ZERO_FP: tuple[tuple[float, float], ...] = tuple(
    (f, a) for f in (0.1, 0.01, 0.001) for a in (0.05, 0.01)
)
ZERO_MISS: tuple[tuple[float, float], ...] = tuple(
    (r, a) for r in (0.5, 0.8, 0.9, 0.95, 0.99) for a in (0.05, 0.01)
)
CLUSTER_TRACES = 3
FPR_POWER: tuple[tuple[float, float, float], ...] = (
    (0.01, 0.002, 0.8),
    (0.01, 0.005, 0.8),
    (0.05, 0.02, 0.9),
    (0.001, 0.0002, 0.8),
)
RECALL_POWER: tuple[tuple[float, float, float], ...] = (
    (0.8, 0.9, 0.8),
    (0.9, 0.97, 0.8),
    (0.5, 0.7, 0.9),
    (0.95, 0.99, 0.8),
)
JOINT: tuple[dict[str, Any], ...] = (
    {
        "min_recall": 0.8,
        "max_fpr": 0.01,
        "true_recall": 0.9,
        "true_fpr": 0.002,
        "split": "bonferroni",
    },
    {"min_recall": 0.8, "max_fpr": 0.01, "true_recall": 0.9, "true_fpr": 0.002, "split": "sidak"},
    {
        "min_recall": 0.9,
        "max_fpr": 0.001,
        "true_recall": None,
        "true_fpr": None,
        "split": "bonferroni",
    },
)
JOINT_POWER = 0.8
MORE_POWER = 0.8
MORE_FPR: tuple[tuple[int, int, float], ...] = ((1, 300, 0.01), (4, 600, 0.01), (6, 200, 0.05))
MORE_RECALL: tuple[tuple[int, int, float], ...] = ((95, 100, 0.9), (40, 45, 0.8), (18, 20, 0.75))
TWO_LOOK_FPR = (0.01, (100, 300, 1000))
TWO_LOOK_RECALL = (0.9, (20, 50, 100))
BUDGET: dict[str, Any] = {
    "population": "binormal_d3",
    "prevalence": 0.001,
    "reviews_per_10k": 20,
    "pilot_neg": 10_000,
    "pilot_pos": 300,
    "min_recall": 0.3,
    "power": 0.8,
    "method": "composite",
    "n_neg_grid": (5000, 20000),
    "n_pos_grid": (100, 300),
}


@dataclass(frozen=True)
class Settings:
    """Simulation sizes of one run mode."""

    mode: str
    sims: int
    cluster_sims: int
    budget_pilots: int
    budget_plan_sims: int
    budget_val_sims: int
    budget_reps: int


FULL = Settings("full", 100_000, 2_000, 5, 400, 2_000, 200)
QUICK = Settings("quick", 4_000, 300, 2, 40, 60, 50)


# ---------------------------------------------------------------- decision rule


def one_sided(metric: Metric, events: ArrayLike, runs: ArrayLike, alpha: float) -> FloatArray:
    """The exact one-sided bound: upper on the FPR, lower on recall (events are misses)."""
    ev = np.asarray(events, dtype=np.float64)
    n = np.asarray(runs, dtype=np.float64)
    if metric == "fpr":
        return intervals.clopper_pearson(ev, n, alpha, side="upper")[1]
    return intervals.clopper_pearson(n - ev, n, alpha, side="lower")[0]


def meets_target(metric: Metric, bound: ArrayLike, target: float) -> BoolArray:
    """Whether a bound certifies: an FPR upper bound at most, or a recall lower bound at least,
    the target."""
    b = np.asarray(bound, dtype=np.float64)
    return np.asarray(b <= target if metric == "fpr" else b >= target, dtype=np.bool_)


def certify(metric: Metric, events: IntArray, runs: int, target: float, alpha: float) -> BoolArray:
    """Decision for many simulated studies of one size (the bound once per distinct count)."""
    values, inverse = np.unique(events, return_inverse=True)
    return meets_target(metric, one_sided(metric, values, runs, alpha), target)[inverse]


def certify_probability(
    metric: Metric, events0: int, runs0: int, extra: int, p_event: float, target: float
) -> float:
    """Exact P(certify on all runs) after ``extra`` more runs with Binomial(extra, p_event)
    events."""
    new = np.arange(extra + 1)
    ok = meets_target(metric, one_sided(metric, events0 + new, runs0 + extra, ALPHA), target)
    return float(stats.binom.pmf(new[ok], extra, p_event).sum())


def event_rate(metric: Metric, rate: float) -> float:
    """Per-run event probability: the FPR, or the miss rate ``1 - recall``."""
    return rate if metric == "fpr" else 1.0 - rate


def power_of(
    metric: Metric, runs: ArrayLike, bound: float, rate: float, alpha: float
) -> FloatArray:
    """The module's exact power (probability of certifying) at a true rate."""
    if metric == "fpr":
        return fpr_power(runs, bound, rate, alpha)
    return recall_power(runs, bound, rate, alpha)


# ---------------------------------------------------------------- rows


def row_rng(seed: int, key: str) -> tuple[np.random.Generator, list[int]]:
    """A generator for one row, seeded from the base seed and a hash of the row id."""
    digest = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "little")
    entropy = [seed, digest]
    return np.random.default_rng(np.random.SeedSequence(entropy)), entropy


def new_row(
    key: str,
    calculator: str,
    check: str,
    params: dict[str, Any],
    runs: dict[str, int],
    true_rates: dict[str, float],
    entropy: list[int],
    *,
    guaranteed: bool = True,
    exact: float | None = None,
    note: str | None = None,
) -> Row:
    return {
        "id": key,
        "calculator": calculator,
        "check": check,
        "guaranteed": guaranteed,
        "params": params,
        "runs": runs,
        "true_rates": true_rates,
        "exact": exact,
        "seed": entropy,
        "note": note,
    }


def finish(row: Row, hits: BoolArray, target: float, target_mcse: float = 0.0) -> Row:
    """Record the simulated share of ``hits`` against ``target``.

    ``exact_meets`` compares the exact value (when there is one) with the
    target without Monte Carlo error, so a row whose exact value sits on the
    target and whose simulated value misses by noise alone can be told apart.
    """
    sims = int(hits.size)
    value = float(hits.mean())
    mcse = math.sqrt(value * (1.0 - value) / sims)
    margin = 2.0 * math.hypot(mcse, target_mcse)
    exact = row.get("exact")
    row.update(
        target=target,
        target_mcse=target_mcse if target_mcse else None,
        simulated=value,
        mcse=mcse,
        sims=sims,
        meets=bool(value >= target - margin),
        exact_meets=None if exact is None else bool(exact >= target),
    )
    return row


def zero_event_rows(cfg: Settings, seed: int) -> list[Row]:
    rows: list[Row] = []
    cases: list[tuple[Metric, str, float, float]] = [
        ("fpr", "zero_fp_runs", f, a) for f, a in ZERO_FP
    ]
    cases += [("recall", "zero_miss_runs", r, a) for r, a in ZERO_MISS]
    for metric, name, bound, alpha in cases:
        n = zero_fp_runs(bound, alpha) if metric == "fpr" else zero_miss_runs(bound, alpha)
        label = "max_fpr" if metric == "fpr" else "min_recall"
        key = f"{name}|{label}={bound:g}|alpha={alpha:g}|confidence"
        rng, entropy = row_rng(seed, key)
        events = rng.binomial(n, event_rate(metric, bound), size=cfg.sims)
        hits = ~certify(metric, events, n, bound, alpha)
        exact = 1.0 - float(power_of(metric, n, bound, bound, alpha))
        side = "n_neg" if metric == "fpr" else "n_pos"
        row = new_row(
            key,
            name,
            "confidence",
            {label: bound, "alpha": alpha},
            {side: n},
            {metric: bound},
            entropy,
            exact=exact,
        )
        rows.append(finish(row, hits, 1.0 - alpha))
    return rows


def clustered_rows(cfg: Settings, seed: int) -> list[Row]:
    """Zero-event plans analysed trace by trace with cluster_cp (CLUSTER_NOTE's mapping)."""
    rows: list[Row] = []
    m = CLUSTER_TRACES
    cases: tuple[tuple[Metric, float], ...] = (("fpr", 0.01), ("recall", 0.9))
    for metric, bound in cases:
        n = zero_fp_runs(bound, ALPHA) if metric == "fpr" else zero_miss_runs(bound, ALPHA)
        name = "zero_fp_runs" if metric == "fpr" else "zero_miss_runs"
        label = "max_fpr" if metric == "fpr" else "min_recall"
        key = f"{name}|{label}={bound:g}|alpha={ALPHA:g}|traces_per_run={m}|clustered_confidence"
        rng, entropy = row_rng(seed, key)
        runs = np.repeat(np.arange(n), m)
        rows_idx = np.arange(n)
        hits = np.empty(cfg.cluster_sims, dtype=np.bool_)
        for s in range(cfg.cluster_sims):
            partial = rng.random((n, m)) < 0.5
            pick = rng.integers(0, m, size=n)
            # the analysis spends alpha / 2 per end, so 2 * ALPHA gives a one-sided ALPHA
            if metric == "fpr":
                # a run is a false positive when any of its traces is flagged
                bad = rng.random(n) < bound
                partial[rows_idx, pick] = True
                flags = partial & bad[:, None]
                est = rate_interval(flags.ravel(), runs, method="cluster_cp", alpha=2 * ALPHA)
                hits[s] = est.upper > bound  # not certified
            else:
                # a run is detected only when every one of its traces is flagged
                detected = rng.random(n) < bound
                partial[rows_idx, pick] = False
                flags = np.where(detected[:, None], True, partial)
                est = rate_interval(flags.ravel(), runs, method="cluster_cp", alpha=2 * ALPHA)
                hits[s] = est.lower < bound  # not certified
        side = "n_neg" if metric == "fpr" else "n_pos"
        params = {
            label: bound,
            "alpha": ALPHA,
            "traces_per_run": m,
            "analysis": "estimate.fixed.rate_interval, method cluster_cp, alpha 2 * alpha",
        }
        row = new_row(
            key,
            name,
            "clustered_confidence",
            params,
            {side: n, "traces": n * m},
            {f"run_level_{metric}": bound},
            entropy,
            exact=1.0 - float(power_of(metric, n, bound, bound, ALPHA)),
        )
        rows.append(finish(row, hits, 1.0 - ALPHA))
    return rows


def plan_rows(cfg: Settings, seed: int) -> list[Row]:
    rows: list[Row] = []
    cases: list[tuple[Metric, float, float, float]] = [("fpr", f, t, p) for f, t, p in FPR_POWER]
    cases += [("recall", r, t, p) for r, t, p in RECALL_POWER]
    for metric, bound, assumed, power in cases:
        plan: RunPlan = (
            fpr_runs_for_power(bound, assumed, power, ALPHA)
            if metric == "fpr"
            else recall_runs_for_power(bound, assumed, power, ALPHA)
        )
        assert plan.horizon is not None
        name = "fpr_runs_for_power" if metric == "fpr" else "recall_runs_for_power"
        labels = ("max_fpr", "true_fpr") if metric == "fpr" else ("min_recall", "true_recall")
        params = {
            labels[0]: bound,
            labels[1]: assumed,
            "power": power,
            "alpha": ALPHA,
            "plan_runs": plan.runs,
            "first_runs": plan.first_runs,
            "horizon": plan.horizon,
            "events_allowed": plan.events_allowed,
            "plan_min_power": plan.min_power,
        }
        span = np.arange(plan.runs, plan.horizon + 1)
        lowest = int(span[np.argmin(power_of(metric, span, bound, assumed, ALPHA))])
        side = "n_neg" if metric == "fpr" else "n_pos"
        base = f"{name}|{labels[0]}={bound:g}|{labels[1]}={assumed:g}|power={power:g}"
        # power at the plan, at its weakest point under the stays-above rule, and at
        # the horizon; confidence at the plan (true rate on the target)
        checks = [
            ("power", plan.runs, assumed, power),
            ("power_lowest_in_horizon", lowest, assumed, power),
            ("power_at_horizon", plan.horizon, assumed, power),
            ("confidence", plan.runs, bound, 1.0 - ALPHA),
        ]
        for check, n, rate, target in checks:
            key = f"{base}|{check}"
            rng, entropy = row_rng(seed, key)
            events = rng.binomial(n, event_rate(metric, rate), size=cfg.sims)
            ok = certify(metric, events, n, bound, ALPHA)
            p_cert = float(power_of(metric, n, bound, rate, ALPHA))
            is_conf = check == "confidence"
            row = new_row(
                key,
                name,
                check,
                params,
                {side: n},
                {metric: rate},
                entropy,
                exact=1.0 - p_cert if is_conf else p_cert,
            )
            rows.append(finish(row, ~ok if is_conf else ok, target))
    return rows


def _coverage_probability(metric: Metric, runs: int, target: float, alpha: float) -> float:
    """Exact P(the one-sided bound covers the true rate) when the true rate equals the target."""
    k = np.arange(runs + 1)
    bound = one_sided(metric, k, runs, alpha)
    covers = bound >= target if metric == "fpr" else bound <= target
    return float(stats.binom.pmf(k[covers], runs, event_rate(metric, target)).sum())


def joint_rows(cfg: Settings, seed: int) -> list[Row]:
    rows: list[Row] = []
    for spec in JOINT:
        r, f = float(spec["min_recall"]), float(spec["max_fpr"])
        plan = joint_plan(
            r,
            f,
            ALPHA,
            true_recall=spec["true_recall"],
            true_fpr=spec["true_fpr"],
            power=JOINT_POWER,
            split=str(spec["split"]),
        )
        params = {
            **spec,
            "alpha": ALPHA,
            "power": JOINT_POWER if spec["true_recall"] is not None else None,
            "alpha_recall": plan.alpha_recall,
            "alpha_fpr": plan.alpha_fpr,
            "plan_joint_power": plan.joint_power,
        }
        runs = {"n_pos": plan.n_pos, "n_neg": plan.n_neg}
        base = f"joint_plan|min_recall={r:g}|max_fpr={f:g}|split={spec['split']}"
        if spec["true_recall"] is not None:
            base += f"|true_recall={spec['true_recall']:g}|true_fpr={spec['true_fpr']:g}"
        # simultaneous coverage when both true rates equal their targets
        key = f"{base}|simultaneous_coverage"
        rng, entropy = row_rng(seed, key)
        misses = rng.binomial(plan.n_pos, 1.0 - r, size=cfg.sims)
        fps = rng.binomial(plan.n_neg, f, size=cfg.sims)
        lo = one_sided("recall", misses, plan.n_pos, plan.alpha_recall)
        hi = one_sided("fpr", fps, plan.n_neg, plan.alpha_fpr)
        hits = np.asarray((lo <= r) & (hi >= f), dtype=np.bool_)
        exact = _coverage_probability("recall", plan.n_pos, r, plan.alpha_recall) * (
            _coverage_probability("fpr", plan.n_neg, f, plan.alpha_fpr)
        )
        row = new_row(
            key,
            "joint_plan",
            "simultaneous_coverage",
            params,
            runs,
            {"recall": r, "fpr": f},
            entropy,
            exact=exact,
        )
        rows.append(finish(row, hits, 1.0 - ALPHA))
        if plan.joint_power is not None:
            tr, tf = float(spec["true_recall"]), float(spec["true_fpr"])
            key = f"{base}|joint_power"
            rng, entropy = row_rng(seed, key)
            misses = rng.binomial(plan.n_pos, 1.0 - tr, size=cfg.sims)
            fps = rng.binomial(plan.n_neg, tf, size=cfg.sims)
            hits = certify("recall", misses, plan.n_pos, r, plan.alpha_recall) & certify(
                "fpr", fps, plan.n_neg, f, plan.alpha_fpr
            )
            row = new_row(
                key,
                "joint_plan",
                "joint_power",
                params,
                runs,
                {"recall": tr, "fpr": tf},
                entropy,
                exact=plan.joint_power,
            )
            rows.append(finish(row, hits, JOINT_POWER))
    return rows


def more_runs_rows(cfg: Settings, seed: int) -> list[Row]:
    rows: list[Row] = []
    cases: list[tuple[Metric, int, int, float]] = [("fpr", k, n, f) for k, n, f in MORE_FPR]
    cases += [("recall", n - d, n, r) for d, n, r in MORE_RECALL]
    for metric, events, n, bound in cases:
        if metric == "fpr":
            out = more_runs_fpr(events, n, bound, ALPHA, power=MORE_POWER)
            name, state = "more_runs_fpr", {"false_positives": events, "runs": n, "max_fpr": bound}
        else:
            out = more_runs_recall(n - events, n, bound, ALPHA, power=MORE_POWER)
            name, state = (
                "more_runs_recall",
                {"detected": n - events, "runs": n, "min_recall": bound},
            )
        if out.for_power is None:
            raise SystemExit(f"grid error: {name} {state} has no for_power answer")
        extra = out.for_power
        rate = events / n
        key = f"{name}|" + "|".join(f"{k}={v:g}" for k, v in state.items()) + "|conditional_power"
        rng, entropy = row_rng(seed, key)
        new = rng.binomial(extra, rate, size=cfg.sims)
        hits = certify(metric, events + new, n + extra, bound, ALPHA)
        params = {
            **state,
            "alpha": ALPHA,
            "power": MORE_POWER,
            "certified_now": out.certified,
            "if_no_events": out.if_no_events,
            "at_observed_rate": out.at_observed_rate,
            "for_power": out.for_power,
        }
        side = "n_neg" if metric == "fpr" else "n_pos"
        row = new_row(
            key,
            name,
            "conditional_power",
            params,
            {side: n + extra, "added": extra},
            {f"observed_{'fpr' if metric == 'fpr' else 'miss_rate'}": rate},
            entropy,
            exact=certify_probability(metric, events, n, extra, rate, bound),
        )
        rows.append(finish(row, hits, MORE_POWER))
    return rows


def _advice(
    cache: dict[int, int | None], metric: Metric, events: int, n0: int, bound: float
) -> int | None:
    """Runs to add after a look that did not certify (more_runs_* ``for_power``), cached."""
    if events not in cache:
        if metric == "fpr":
            out = more_runs_fpr(events, n0, bound, ALPHA, power=MORE_POWER)
        else:
            out = more_runs_recall(n0 - events, n0, bound, ALPHA, power=MORE_POWER)
        cache[events] = out.for_power
    return cache[events]


def two_look_rows(cfg: Settings, seed: int) -> list[Row]:
    rows: list[Row] = []
    note = (
        "Diagnostic, not a documented guarantee: the certificate on all runs treats the total "
        "as fixed in advance; see 'Adding runs after a look' in baserate.certify.samplesize."
    )
    cases: list[tuple[Metric, float, int]] = [("fpr", TWO_LOOK_FPR[0], n) for n in TWO_LOOK_FPR[1]]
    cases += [("recall", TWO_LOOK_RECALL[0], n) for n in TWO_LOOK_RECALL[1]]
    for metric, bound, n0 in cases:
        name = "more_runs_fpr" if metric == "fpr" else "more_runs_recall"
        label = "max_fpr" if metric == "fpr" else "min_recall"
        key = f"{name}|{label}={bound:g}|n0={n0}|two_look_confidence"
        rng, entropy = row_rng(seed, key)
        p = event_rate(metric, bound)
        cache: dict[int, int | None] = {}
        first = rng.binomial(n0, p, size=cfg.sims)
        certified = np.zeros(cfg.sims, dtype=np.bool_)
        for k in np.unique(first).tolist():
            idx = first == k
            if bool(certify(metric, np.array([k]), n0, bound, ALPHA)[0]):
                certified[idx] = True
                continue
            extra = _advice(cache, metric, k, n0, bound)
            if extra is None:  # unreachable at the observed rate: stop without a claim
                continue
            new = rng.binomial(extra, p, size=int(idx.sum()))
            certified[idx] = certify(metric, k + new, n0 + extra, bound, ALPHA)
        # exact false-certification probability over every first-look count
        false_cert = 0.0
        ks = np.arange(n0 + 1)
        pk = stats.binom.pmf(ks, n0, p)
        first_ok = meets_target(metric, one_sided(metric, ks, n0, ALPHA), bound)
        for k in ks[pk > 1e-13].tolist():
            if first_ok[k]:
                false_cert += float(pk[k])
                continue
            extra = _advice(cache, metric, k, n0, bound)
            if extra is not None:
                false_cert += float(pk[k]) * certify_probability(metric, k, n0, extra, p, bound)
        params = {
            label: bound,
            "alpha": ALPHA,
            "n0": n0,
            "power": MORE_POWER,
            "procedure": "look at n0; stop if certified; else add for_power runs and certify",
            "added_runs_by_first_look_events": {str(k): v for k, v in sorted(cache.items())},
        }
        side = "n_neg" if metric == "fpr" else "n_pos"
        row = new_row(
            key,
            name,
            "two_look_confidence",
            params,
            {side: n0},
            {metric: bound},
            entropy,
            guaranteed=False,
            exact=1.0 - false_cert,
            note=note,
        )
        rows.append(finish(row, ~certified, 1.0 - ALPHA))
    return rows


def budget_rows(cfg: Settings, seed: int) -> list[Row]:
    """Pilot-based planning compared with the population it is meant to predict.

    A design's true power does not depend on the pilot, so it is simulated
    once per design from fresh population studies. Each of
    ``cfg.budget_pilots`` independent pilots gives its own plan; a design's
    target is its planned power averaged over the pilots, with the standard
    error of that mean (pilot-to-pilot spread) as ``target_mcse``.
    """
    rows: list[Row] = []
    model = MODELS[str(BUDGET["population"])]
    prevalence = float(BUDGET["prevalence"])
    flag_rate = flag_rate_from_budget(float(BUDGET["reviews_per_10k"]))
    min_recall = float(BUDGET["min_recall"])
    method = str(BUDGET["method"])
    target_power = float(BUDGET["power"])
    grid = [(int(a), int(b)) for a in BUDGET["n_neg_grid"] for b in BUDGET["n_pos_grid"]]
    exact = truth(model, prevalence, flag_rate)
    assert model.pos is not None and model.neg is not None

    def draw(rng: np.random.Generator, n_neg: int, n_pos: int) -> tuple[FloatArray, FloatArray]:
        neg = np.asarray(model.neg.rvs(size=n_neg, random_state=rng), dtype=np.float64)
        pos = np.asarray(model.pos.rvs(size=n_pos, random_state=rng), dtype=np.float64)
        return neg, pos

    plans: list[BudgetPlan] = []
    pilot_seeds: list[list[int]] = []
    plan_seeds = [seed + i for i in range(cfg.budget_pilots)]
    for i, plan_seed in enumerate(plan_seeds):
        rng, entropy = row_rng(seed, f"plan_budget|pilot={i}")
        pilot_neg, pilot_pos = draw(rng, int(BUDGET["pilot_neg"]), int(BUDGET["pilot_pos"]))
        pilot_seeds.append(entropy)
        plans.append(
            plan_budget(
                pilot_neg,
                pilot_pos,
                prevalence,
                flag_rate,
                min_recall=min_recall,
                power=target_power,
                n_neg_grid=tuple(BUDGET["n_neg_grid"]),
                n_pos_grid=tuple(BUDGET["n_pos_grid"]),
                method=method,
                sims=cfg.budget_plan_sims,
                reps=cfg.budget_reps,
                seed=plan_seed,
            )
        )
    source = json.loads(
        resources.files("baserate.estimate").joinpath(support.DATA_FILE).read_text("utf-8")
    ).get("source")
    common = {
        **{k: (list(v) if isinstance(v, tuple) else v) for k, v in BUDGET.items()},
        "alpha": ALPHA,
        "flag_rate": flag_rate,
        "true_recall_at_budget": exact.budget_recall,
        "pilots": cfg.budget_pilots,
        "pilot_recalls_at_budget": [p.pilot.recall for p in plans],
        "pilot_tail_neg": [p.pilot_tail_neg for p in plans],
        "pilot_seeds": pilot_seeds,
        "plan_seeds": plan_seeds,
        "plan_sims": cfg.budget_plan_sims,
        "reps": cfg.budget_reps,
        "e0_unsupported_source": source,
    }
    note = "Pilot-resampling estimate, not a documented guarantee; see baserate.certify.atbudget."
    true_power: dict[tuple[int, int], float] = {}
    for j, (n_neg, n_pos) in enumerate(grid):
        key = f"plan_budget|n_neg={n_neg}|n_pos={n_pos}|power_calibration"
        rng, entropy = row_rng(seed, key)
        hits = np.empty(cfg.budget_val_sims, dtype=np.bool_)
        lowers = np.empty(cfg.budget_val_sims)
        for s in range(cfg.budget_val_sims):
            neg, pos = draw(rng, n_neg, n_pos)
            est = recall_at_budget(
                neg,
                pos,
                prevalence,
                flag_rate,
                method,
                ALPHA,
                reps=cfg.budget_reps,
                rng=rng,
                warn=False,
            )
            lowers[s] = est.recall.lower
            hits[s] = est.recall.lower >= min_recall
        true_power[(n_neg, n_pos)] = float(hits.mean())
        designs = [p.designs[j] for p in plans]
        assert all((d.n_neg, d.n_pos) == (n_neg, n_pos) for d in designs)
        planned = np.array([d.power for d in designs])
        spread = float(planned.std(ddof=1)) / math.sqrt(planned.size) if planned.size > 1 else 0.0
        target_mcse = spread if planned.size > 1 else designs[0].mcse
        params = {
            **common,
            "planned_powers": planned.tolist(),
            "planned_mcse_each": [d.mcse for d in designs],
            "planned_median_lower_each": [d.median_lower for d in designs],
            "true_median_lower": float(np.median(lowers)),
            "e0_unsupported_cells": list(
                support.check(method, prevalence, flag_rate, n_neg, n_pos, False, emit=False)
            ),
        }
        row = new_row(
            key,
            "plan_budget",
            "power_calibration",
            params,
            {"n_neg": n_neg, "n_pos": n_pos},
            {"recall_at_budget": exact.budget_recall},
            entropy,
            guaranteed=False,
            note=note + " Target: planned power averaged over the pilots.",
        )
        rows.append(finish(row, hits, float(planned.mean()), target_mcse))
    # the design each pilot's plan chooses, at its true power
    chosen = [None if p.best is None else (p.best.n_neg, p.best.n_pos) for p in plans]
    picked = [c for c in chosen if c is not None]
    key = "plan_budget|chosen_design|power"
    _, entropy = row_rng(seed, key)
    row = new_row(
        key,
        "plan_budget",
        "chosen_design_power",
        {**common, "chosen_designs": [None if c is None else list(c) for c in chosen]},
        {},
        {"recall_at_budget": exact.budget_recall},
        entropy,
        guaranteed=False,
        note=note
        + " Simulated: the true power of each pilot's chosen design, averaged over the pilots"
        " that chose one (from the calibration rows' studies).",
    )
    if picked:
        value = sum(true_power[c] for c in picked) / len(picked)
        share = {c: picked.count(c) / len(picked) for c in set(picked)}
        var = sum(w**2 * true_power[c] * (1 - true_power[c]) for c, w in share.items())
        mcse = math.sqrt(var / cfg.budget_val_sims)
        meets = value >= target_power - 2 * mcse
    else:
        value, mcse, meets = None, None, False
    row.update(
        target=target_power,
        target_mcse=None,
        simulated=value,
        mcse=mcse,
        sims=cfg.budget_val_sims,
        meets=bool(meets),
        exact_meets=None,
    )
    rows.append(row)
    return rows


# ---------------------------------------------------------------- run


def _git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return out.stdout.strip() if out.returncode == 0 else ""


def summarize(rows: list[Row]) -> dict[str, Any]:
    guaranteed = [r for r in rows if r["guaranteed"]]
    other = [r for r in rows if not r["guaranteed"]]
    with_exact = [r for r in rows if r.get("exact_meets") is not None]
    return {
        "rows": len(rows),
        "meet": sum(bool(r["meets"]) for r in rows),
        "guaranteed_rows": len(guaranteed),
        "guaranteed_meet": sum(bool(r["meets"]) for r in guaranteed),
        "other_rows": len(other),
        "other_meet": sum(bool(r["meets"]) for r in other),
        "rows_with_exact": len(with_exact),
        "exact_meet": sum(bool(r["exact_meets"]) for r in with_exact),
        "not_meeting": [r["id"] for r in rows if not r["meets"]],
        "exact_not_meeting": [r["id"] for r in with_exact if not r["exact_meets"]],
    }


SECTIONS: tuple[tuple[str, Callable[[Settings, int], list[Row]]], ...] = (
    ("zero_event", zero_event_rows),
    ("clustered", clustered_rows),
    ("power_plans", plan_rows),
    ("joint_plans", joint_rows),
    ("more_runs", more_runs_rows),
    ("two_looks", two_look_rows),
    ("budget", budget_rows),
)


def run(cfg: Settings, seed: int) -> tuple[list[Row], dict[str, float]]:
    rows: list[Row] = []
    seconds: dict[str, float] = {}
    for name, section in SECTIONS:
        start = time.perf_counter()
        rows.extend(section(cfg, seed))
        seconds[name] = round(time.perf_counter() - start, 2)
        print(f"{name}: {seconds[name]} s", flush=True)
    return rows, seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("out", type=Path, help="output JSON path")
    parser.add_argument("--quick", action="store_true", help="fewer simulations (seconds)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="base seed")
    args = parser.parse_args(argv)
    cfg = QUICK if args.quick else FULL
    started = datetime.now(UTC)
    t0 = time.perf_counter()
    rows, seconds = run(cfg, args.seed)
    summary = summarize(rows)
    out = {
        "artifact": "certify_validation",
        "description": (
            "Simulation study of the baserate.certify calculators (RQ2). Synthetic data only: "
            "binomial outcomes and binormal scores, no monitor or model results."
        ),
        "synthetic": True,
        "mode": cfg.mode,
        "criterion": (
            "meets: simulated >= target - 2 * mcse (combined with target_mcse when the target "
            "is itself simulated)"
        ),
        "settings": asdict(cfg),
        "grid": {
            "zero_fp_runs": ZERO_FP,
            "zero_miss_runs": ZERO_MISS,
            "clustered_traces_per_run": CLUSTER_TRACES,
            "fpr_runs_for_power": FPR_POWER,
            "recall_runs_for_power": RECALL_POWER,
            "joint_plan": JOINT,
            "joint_power": JOINT_POWER,
            "more_runs_power": MORE_POWER,
            "more_runs_fpr": MORE_FPR,
            "more_runs_recall": MORE_RECALL,
            "two_look_fpr": TWO_LOOK_FPR,
            "two_look_recall": TWO_LOOK_RECALL,
            "budget": BUDGET,
            "alpha": ALPHA,
        },
        "summary": summary,
        "rows": rows,
        "provenance": {
            "git_sha": _git("rev-parse", "HEAD") or None,
            "git_dirty_src_tools": bool(_git("status", "--porcelain", "--", "src", "tools")),
            "seed": args.seed,
            "seeding": "each row: numpy SeedSequence([seed, first 8 bytes of sha256(row id)])",
            "command": " ".join([Path(sys.executable).name, *sys.argv]),
            "started_at": started.isoformat(timespec="seconds"),
            "seconds": round(time.perf_counter() - t0, 1),
            "section_seconds": seconds,
            "environment": {
                "python": platform.python_version(),
                "numpy": np.__version__,
                "scipy": scipy.__version__,
                "platform": platform.platform(),
                "cpus": os.cpu_count(),
            },
            "models": "none (pure simulation; no model calls)",
            "usage": {"model_calls": 0, "cash_usd": 0},
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(
        f"certify validation ({cfg.mode}): {summary['meet']} of {summary['rows']} rows meet "
        f"their target; documented guarantees {summary['guaranteed_meet']} of "
        f"{summary['guaranteed_rows']}; wrote {args.out}"
    )
    for rid in summary["not_meeting"]:
        print(f"  not meeting: {rid}")
    return 0 if summary["guaranteed_meet"] == summary["guaranteed_rows"] else 1


if __name__ == "__main__":
    sys.exit(main())
