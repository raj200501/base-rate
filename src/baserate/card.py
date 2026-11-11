"""Monitor report card: what a set of labeled scores supports, on one page.

:func:`build_card` computes every number on the card from a
:class:`baserate.scores.ScoreSet`; nothing on it is typed in. Sections:

1. **Data.** Traces and independent clusters per label, the synthetic share
   (when the scores have a ``synthetic`` column), and the smallest
   false-positive rate the benign clusters could certify at all: the
   one-sided bound when none of them has a false positive, about ``3 / n``
   at 95%.
2. **Recall and precision at a review budget.** For review budgets of 5, 20,
   and 100 per 10,000 trajectories and prevalences of 1e-2, 1e-3, and 1e-4:
   recall, precision, and the false-positive rate when the top-scoring
   ``budget / 10,000`` of traffic is reviewed
   (:func:`baserate.estimate.recall_at_budget`, composite intervals by
   default), with any E0 support warning for the row.
3. **Decision threshold** (when one is given). The false-positive rate and
   recall when traces scoring at or above the threshold are flagged, with
   the cluster-aware exact bounds of :func:`baserate.estimate.fixed_threshold`
   (``cluster_cp``), and precision at each prevalence.
4. **Certification gap.** For each budget, the false-positive rate that fits
   it (``budget / 10,000``) and how many more independent benign runs with no
   false positive are needed to claim it (:func:`baserate.certify.more_runs_fpr`).
   A benign cluster counts as a false positive if any of its traces is
   flagged, as in ``cluster_cp``.

Sections 3 and 4 assume an unweighted sample of independent runs, so they
are left out (with the reason stated) when the scores carry sampling weights.
:func:`render_markdown` and :func:`render_json` format a card; the JSON form
(:meth:`Card.to_dict`) holds the same numbers at full precision.
"""

from __future__ import annotations

import json
import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any

import numpy as np
from numpy.typing import NDArray

from baserate import __version__
from baserate.certify import more_runs_fpr
from baserate.estimate import (
    BUDGET_METHODS,
    Interval,
    UnsupportedCellWarning,
    fixed_threshold,
    flag_rate_from_budget,
    recall_at_budget,
)
from baserate.estimate.intervals import clopper_pearson, zero_event_upper
from baserate.scores import ScoreSet

SCHEMA = "baserate-card/1"
"""Schema id of the JSON card."""

CARD_BUDGETS: tuple[float, ...] = (5, 20, 100)
"""Review budgets per 10,000 trajectories shown on a card."""

CARD_PREVALENCES: tuple[float, ...] = (1e-2, 1e-3, 1e-4)
"""Deployment prevalences shown on a card."""

THRESHOLD_METHOD = "cluster_cp"
"""Interval method for the decision-threshold section (cluster-aware exact bounds)."""

CLUSTER_AWARE_METHODS: frozenset[str] = frozenset(
    {"cluster_cp", "cluster_bootstrap", "bayesian_bootstrap", "composite"}
)
"""Budget interval methods that treat clusters, not traces, as the independent units.

The ``plugin_*`` methods are binomial intervals on the violation traces at
the estimated threshold: they treat traces as independent and ignore the
threshold's own uncertainty (:mod:`baserate.estimate.atbudget`).
"""

PER = 10_000
"""Budgets are stated per this many trajectories."""

WEIGHTS_OMITTED = (
    "The scores carry sampling weights. The decision-threshold bounds and the "
    "certification gap assume an unweighted sample of independent runs, so they "
    "are not computed; the budget table uses the weights."
)

REPRESENTATIVE_NOTE = (
    "These numbers describe the scored sample. They carry over to a deployment only "
    "if the benign and the violation traces each represent that deployment's traffic "
    "of their kind, and only for the monitor version that produced the scores."
)


# ----------------------------------------------------------------- formatting


def fmt_prob(x: float) -> str:
    """A recall, precision, or share for display: 3 decimals."""
    return f"{x:.3f}"


def fmt_small(x: float) -> str:
    """A false-positive rate or bound for display: 3 significant digits (rates can be tiny)."""
    return f"{x:.3g}"


def fmt_interval(iv: Interval, small: bool = False) -> str:
    """``estimate [lower, upper]``, with :func:`fmt_small` if ``small`` else :func:`fmt_prob`."""
    f = fmt_small if small else fmt_prob
    return f"{f(iv.estimate)} [{f(iv.lower)}, {f(iv.upper)}]"


def confidence_text(alpha: float) -> str:
    """Confidence level as a percentage, for example ``95%``."""
    return f"{(1 - alpha) * 100:g}%"


def method_note(method: str) -> str:
    """What a budget interval method counts as independent, in plain words."""
    if method in CLUSTER_AWARE_METHODS:
        return "clusters (source runs) are the independent units"
    return (
        "this method treats traces as independent and ignores the threshold's uncertainty, "
        "so its intervals are too narrow when traces share source runs"
    )


def interval_dict(iv: Interval) -> dict[str, Any]:
    """An interval as a JSON-ready dict."""
    return {
        "estimate": iv.estimate,
        "lower": iv.lower,
        "upper": iv.upper,
        "method": iv.method,
        "alpha": iv.alpha,
    }


def e0_support_source() -> str:
    """Where the packaged E0 support data come from (``source`` of unsupported_cells.json)."""
    text = (
        resources.files("baserate.estimate")
        .joinpath("unsupported_cells.json")
        .read_text(encoding="utf-8")
    )
    data = json.loads(text)
    return str(data.get("source", "unknown"))


# ------------------------------------------------------------------- sections


@dataclass(frozen=True)
class DataSummary:
    """What the scores contain.

    Attributes:
        source: Where the scores came from.
        sha256: SHA-256 of the score file, when read from a file.
        n_traces: All traces.
        n_neg: Benign traces.
        n_pos: Violation traces.
        n_clusters: Distinct clusters over both labels.
        n_neg_clusters: Independent benign clusters.
        n_pos_clusters: Independent violation clusters.
        n_synthetic: Traces marked synthetic, or None without a ``synthetic`` column.
        synthetic_share: ``n_synthetic / n_traces``, or None.
        weighted: Whether sampling weights are present.
        n_stage2: Traces with a stage-2 score, or None without that column.
        comments: Comment lines of the score file.
        fpr_floor: One-sided upper bound on the false-positive rate if none of
            the benign clusters had a false positive: no smaller rate can be
            certified with these benign runs.
    """

    source: str
    sha256: str | None
    n_traces: int
    n_neg: int
    n_pos: int
    n_clusters: int
    n_neg_clusters: int
    n_pos_clusters: int
    n_synthetic: int | None
    synthetic_share: float | None
    weighted: bool
    n_stage2: int | None
    comments: tuple[str, ...]
    fpr_floor: float


@dataclass(frozen=True)
class BudgetRow:
    """Recall, precision, and FPR at one prevalence and review budget.

    Attributes:
        prevalence: Deployment prevalence of violations.
        budget_per_10k: Reviews per 10,000 trajectories.
        flag_rate: ``budget_per_10k / 10,000``.
        threshold: Score threshold at which the budget is met.
        tie_fraction: Fraction of the traces exactly at the threshold that are reviewed.
        recall: Recall with its interval.
        precision: Precision with its interval.
        fpr: False-positive rate with its interval.
        n_neg: Benign traces.
        n_pos: Violation traces.
        n_neg_clusters: Independent benign clusters.
        n_pos_clusters: Independent violation clusters.
        warnings: Ids of E0 cells where the interval method under-covered in a
            configuration like this one (empty when none).
    """

    prevalence: float
    budget_per_10k: float
    flag_rate: float
    threshold: float
    tie_fraction: float
    recall: Interval
    precision: Interval
    fpr: Interval
    n_neg: int
    n_pos: int
    n_neg_clusters: int
    n_pos_clusters: int
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict."""
        return {
            "prevalence": self.prevalence,
            "budget_per_10k": self.budget_per_10k,
            "flag_rate": self.flag_rate,
            "threshold": self.threshold,
            "tie_fraction": self.tie_fraction,
            "recall": interval_dict(self.recall),
            "precision": interval_dict(self.precision),
            "fpr": interval_dict(self.fpr),
            "n_neg": self.n_neg,
            "n_pos": self.n_pos,
            "n_neg_clusters": self.n_neg_clusters,
            "n_pos_clusters": self.n_pos_clusters,
            "e0_unsupported_cells": list(self.warnings),
        }


@dataclass(frozen=True)
class ThresholdSummary:
    """Rates when traces scoring at or above a fixed threshold are flagged.

    Attributes:
        threshold: The decision threshold.
        method: Interval method (``cluster_cp``).
        fpr: False-positive rate per benign trace, with its two-sided interval.
        recall: Recall per violation trace, with its two-sided interval.
        precision: Precision interval at each card prevalence, as (prevalence, interval).
        flagged_neg: Benign traces flagged.
        flagged_pos: Violation traces flagged.
        fp_clusters: Benign clusters with at least one flagged trace.
        detected_clusters: Violation clusters with every trace flagged.
        n_neg_clusters: Independent benign clusters.
        n_pos_clusters: Independent violation clusters.
        fpr_upper: One-sided upper bound on the cluster-level false-positive
            rate (``fp_clusters`` of ``n_neg_clusters``, exact).
    """

    threshold: float
    method: str
    fpr: Interval
    recall: Interval
    precision: tuple[tuple[float, Interval], ...]
    flagged_neg: int
    flagged_pos: int
    fp_clusters: int
    detected_clusters: int
    n_neg_clusters: int
    n_pos_clusters: int
    fpr_upper: float

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict."""
        return {
            "threshold": self.threshold,
            "rule": "flag when score >= threshold",
            "method": self.method,
            "fpr": interval_dict(self.fpr),
            "recall": interval_dict(self.recall),
            "precision": [{"prevalence": p, **interval_dict(iv)} for p, iv in self.precision],
            "flagged_neg": self.flagged_neg,
            "flagged_pos": self.flagged_pos,
            "fp_clusters": self.fp_clusters,
            "detected_clusters": self.detected_clusters,
            "n_neg_clusters": self.n_neg_clusters,
            "n_pos_clusters": self.n_pos_clusters,
            "fpr_upper_one_sided": self.fpr_upper,
        }


@dataclass(frozen=True)
class CertificationGap:
    """How far the benign runs are from certifying one false-positive rate.

    Attributes:
        max_fpr: The false-positive rate to claim.
        budget_per_10k: The review budget it corresponds to (``max_fpr * 10,000``).
        alpha: Miss probability of the one-sided bound.
        runs: Independent benign runs (clusters) so far.
        false_positive_runs: Runs with a false positive at the threshold, or
            None when no threshold was given (then the gap assumes none).
        current_bound: One-sided upper bound on the false-positive rate now.
        certified: Whether the runs already support the claim.
        more_if_none: More runs needed if none of them has a false positive.
        more_at_observed: More runs needed if false positives continue at the
            observed rate; None when that rate never reaches the claim.
        reachable: False when the observed rate is at or above ``max_fpr``.
        sentence: The gap in plain words.
    """

    max_fpr: float
    budget_per_10k: float
    alpha: float
    runs: int
    false_positive_runs: int | None
    current_bound: float
    certified: bool
    more_if_none: int
    more_at_observed: int | None
    reachable: bool
    sentence: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict."""
        return {
            "max_fpr": self.max_fpr,
            "budget_per_10k": self.budget_per_10k,
            "alpha": self.alpha,
            "runs": self.runs,
            "false_positive_runs": self.false_positive_runs,
            "current_bound": self.current_bound,
            "certified": self.certified,
            "more_runs_if_no_false_positive": self.more_if_none,
            "more_runs_at_observed_rate": self.more_at_observed,
            "reachable_at_observed_rate": self.reachable,
            "sentence": self.sentence,
        }


@dataclass(frozen=True)
class Card:
    """A monitor report card.

    Attributes:
        name: The monitor's name.
        version: baserate version that computed the card.
        method: Interval method of the budget table.
        alpha: Miss probability of every interval and bound.
        reps: Resamples for the resampling interval methods.
        seed: Seed of every resampling (one fresh generator per table row).
        budgets: Review budgets per 10,000.
        prevalences: Deployment prevalences.
        threshold_given: The decision threshold, or None.
        data: The data summary.
        budget_rows: One row per prevalence and budget.
        e0_source: Where the E0 support data come from.
        threshold: The decision-threshold section, or None.
        certification: One gap per budget (empty when weights are present).
        notes: Notes given by the caller.
        omitted: Reasons for sections that were not computed.
    """

    name: str
    version: str
    method: str
    alpha: float
    reps: int
    seed: int
    budgets: tuple[float, ...]
    prevalences: tuple[float, ...]
    threshold_given: float | None
    data: DataSummary
    budget_rows: tuple[BudgetRow, ...]
    e0_source: str
    threshold: ThresholdSummary | None
    certification: tuple[CertificationGap, ...]
    notes: tuple[str, ...]
    omitted: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """The card as a JSON-ready dict (schema :data:`SCHEMA`)."""
        d = self.data
        return {
            "schema": SCHEMA,
            "name": self.name,
            "baserate_version": self.version,
            "settings": {
                "budget_method": self.method,
                "budget_method_cluster_aware": self.method in CLUSTER_AWARE_METHODS,
                "threshold_method": THRESHOLD_METHOD,
                "alpha": self.alpha,
                "confidence": 1 - self.alpha,
                "reps": self.reps,
                "seed": self.seed,
                "budgets_per_10k": list(self.budgets),
                "prevalences": list(self.prevalences),
                "threshold": self.threshold_given,
            },
            "data": {
                "source": d.source,
                "sha256": d.sha256,
                "n_traces": d.n_traces,
                "n_neg": d.n_neg,
                "n_pos": d.n_pos,
                "n_clusters": d.n_clusters,
                "n_neg_clusters": d.n_neg_clusters,
                "n_pos_clusters": d.n_pos_clusters,
                "n_synthetic": d.n_synthetic,
                "synthetic_share": d.synthetic_share,
                "weighted": d.weighted,
                "n_stage2": d.n_stage2,
                "comments": list(d.comments),
                "fpr_floor_one_sided": d.fpr_floor,
            },
            "e0_support_source": self.e0_source,
            "budget_table": [r.to_dict() for r in self.budget_rows],
            "decision_threshold": None if self.threshold is None else self.threshold.to_dict(),
            "certification": [g.to_dict() for g in self.certification],
            "notes": list(self.notes),
            "omitted": list(self.omitted),
        }


# ---------------------------------------------------------------- computation


def summarize(scores: ScoreSet, alpha: float = 0.05) -> DataSummary:
    """The data section of a card.

    Args:
        scores: Labeled scores.
        alpha: Miss probability of the one-sided ``fpr_floor`` bound.

    Returns:
        Counts, shares, and the smallest certifiable false-positive rate.
    """
    n_stage2 = (
        None if scores.stage2_scores is None else int(np.sum(~np.isnan(scores.stage2_scores)))
    )
    return DataSummary(
        source=scores.source,
        sha256=scores.sha256,
        n_traces=scores.n_traces,
        n_neg=scores.n_neg,
        n_pos=scores.n_pos,
        n_clusters=scores.n_clusters,
        n_neg_clusters=scores.n_neg_clusters,
        n_pos_clusters=scores.n_pos_clusters,
        n_synthetic=scores.n_synthetic,
        synthetic_share=scores.synthetic_share,
        weighted=scores.weights is not None,
        n_stage2=n_stage2,
        comments=scores.comments,
        fpr_floor=float(zero_event_upper(scores.n_neg_clusters, alpha)),
    )


def budget_table(
    scores: ScoreSet,
    prevalences: Sequence[float] = CARD_PREVALENCES,
    budgets: Sequence[float] = CARD_BUDGETS,
    method: str = "composite",
    alpha: float = 0.05,
    reps: int = 1000,
    seed: int = 0,
) -> tuple[BudgetRow, ...]:
    """Recall, precision, and FPR at every prevalence and review budget.

    Each row calls :func:`baserate.estimate.recall_at_budget` with a fresh
    generator seeded with ``seed``, so a row does not depend on which other
    rows are requested. E0 support warnings are collected on the row instead
    of being emitted.

    Args:
        scores: Labeled scores (clusters and weights are used).
        prevalences: Deployment prevalences, each in (0, 1).
        budgets: Reviews per 10,000 trajectories, each in (0, 10,000].
        method: One of :data:`baserate.estimate.BUDGET_METHODS`.
        alpha: Miss probability of each two-sided interval.
        reps: Resamples for the resampling methods.
        seed: Seed of each row's generator.

    Returns:
        Rows in prevalence-major order.
    """
    if method not in BUDGET_METHODS:
        raise ValueError(f"method must be one of {', '.join(BUDGET_METHODS)}")
    rows: list[BudgetRow] = []
    for prevalence in prevalences:
        for budget in budgets:
            flag_rate = flag_rate_from_budget(budget)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UnsupportedCellWarning)
                est = recall_at_budget(
                    scores.neg,
                    scores.pos,
                    prevalence,
                    flag_rate,
                    method,
                    alpha,
                    neg_clusters=scores.neg_clusters,
                    pos_clusters=scores.pos_clusters,
                    neg_weights=scores.neg_weights,
                    pos_weights=scores.pos_weights,
                    reps=reps,
                    rng=np.random.default_rng(seed),
                )
            rows.append(
                BudgetRow(
                    prevalence=prevalence,
                    budget_per_10k=budget,
                    flag_rate=flag_rate,
                    threshold=est.point.threshold,
                    tie_fraction=est.point.tie_fraction,
                    recall=est.recall,
                    precision=est.precision,
                    fpr=est.fpr,
                    n_neg=est.n_neg,
                    n_pos=est.n_pos,
                    n_neg_clusters=est.n_neg_clusters,
                    n_pos_clusters=est.n_pos_clusters,
                    warnings=est.warnings,
                )
            )
    return tuple(rows)


def _cluster_counts(flags: NDArray[np.bool_], clusters: NDArray[np.str_]) -> tuple[int, int]:
    """(clusters with any trace flagged, clusters with every trace flagged)."""
    any_flag: dict[str, bool] = {}
    all_flag: dict[str, bool] = {}
    for c, f in zip(clusters.tolist(), flags.tolist(), strict=True):
        any_flag[c] = any_flag.get(c, False) or bool(f)
        all_flag[c] = all_flag.get(c, True) and bool(f)
    return sum(any_flag.values()), sum(all_flag.values())


def threshold_summary(
    scores: ScoreSet,
    threshold: float,
    prevalences: Sequence[float] = CARD_PREVALENCES,
    alpha: float = 0.05,
) -> ThresholdSummary:
    """Rates at a fixed decision threshold with cluster-aware exact bounds.

    Args:
        scores: Labeled scores (unweighted).
        threshold: Traces scoring at or above it are flagged.
        prevalences: Prevalences at which to state precision.
        alpha: Miss probability of the two-sided intervals and the one-sided bound.

    Returns:
        The decision-threshold section.
    """
    if not math.isfinite(threshold):
        raise ValueError("threshold must be a finite number")
    if not prevalences:
        raise ValueError("need at least one prevalence")
    neg, pos = scores.neg, scores.pos
    estimates = [
        fixed_threshold(
            neg,
            pos,
            threshold,
            p,
            THRESHOLD_METHOD,
            alpha,
            neg_clusters=scores.neg_clusters,
            pos_clusters=scores.pos_clusters,
        )
        for p in prevalences
    ]
    first = estimates[0]
    fp_clusters, _ = _cluster_counts(neg >= threshold, scores.neg_clusters)
    _, detected = _cluster_counts(pos >= threshold, scores.pos_clusters)
    _, upper = clopper_pearson(fp_clusters, scores.n_neg_clusters, alpha, side="upper")
    return ThresholdSummary(
        threshold=threshold,
        method=THRESHOLD_METHOD,
        fpr=first.fpr,
        recall=first.recall,
        precision=tuple((p, e.precision) for p, e in zip(prevalences, estimates, strict=True)),
        flagged_neg=int(np.sum(neg >= threshold)),
        flagged_pos=int(np.sum(pos >= threshold)),
        fp_clusters=fp_clusters,
        detected_clusters=detected,
        n_neg_clusters=scores.n_neg_clusters,
        n_pos_clusters=scores.n_pos_clusters,
        fpr_upper=float(upper),
    )


def _runs(n: int) -> str:
    return f"{n:,} independent benign run{'' if n == 1 else 's'}"


def certification_gap(
    runs: int,
    false_positive_runs: int | None,
    budget_per_10k: float,
    alpha: float = 0.05,
) -> CertificationGap:
    """The certification gap for the false-positive rate that fits a review budget.

    Args:
        runs: Independent benign runs (clusters).
        false_positive_runs: Runs with a false positive, or None when unknown
            (no threshold); the gap then assumes none and is a lower bound.
        budget_per_10k: Review budget; the claim is FPR <= budget / 10,000.
        alpha: Miss probability of the one-sided bound.

    Returns:
        The gap, with a plain-words sentence.
    """
    max_fpr = flag_rate_from_budget(budget_per_10k)
    events = 0 if false_positive_runs is None else false_positive_runs
    more = more_runs_fpr(events, runs, max_fpr, alpha)
    conf = confidence_text(alpha)
    claim = (
        f"a false-positive rate of at most {max_fpr:g} ({budget_per_10k:g} per {PER:,}) at {conf}"
    )
    bound = fmt_small(more.current_bound)
    if false_positive_runs is None:
        if more.if_no_events == 0:
            sentence = (
                f"If none of the {_runs(runs)} had a false positive, these data would support "
                f"{claim}; give a decision threshold to check."
            )
        else:
            sentence = (
                f"No decision threshold was given, so false positives were not counted. Even if "
                f"none of the {_runs(runs)} had one, {more.if_no_events:,} more independent "
                f"benign runs with no false positive would be needed to claim {claim}."
            )
    elif more.certified:
        sentence = (
            f"These data already support {claim}: {events} of {_runs(runs)} had a false "
            f"positive (one-sided upper bound {bound})."
        )
    else:
        sentence = (
            f"{more.if_no_events:,} more independent benign runs with no false positive are "
            f"needed to claim {claim} (now {events} of {_runs(runs)} had a false positive; "
            f"one-sided upper bound {bound})."
        )
        if events > 0 and more.at_observed_rate is not None:
            sentence += (
                f" If false positives continue at the observed rate, about "
                f"{more.at_observed_rate:,} more are needed."
            )
        elif events > 0:
            sentence += (
                f" If false positives continue at the observed rate of {events} in {runs:,}, no "
                "number of added runs reaches it."
            )
    return CertificationGap(
        max_fpr=max_fpr,
        budget_per_10k=budget_per_10k,
        alpha=alpha,
        runs=runs,
        false_positive_runs=false_positive_runs,
        current_bound=more.current_bound,
        certified=more.certified,
        more_if_none=more.if_no_events,
        more_at_observed=more.at_observed_rate,
        reachable=more.reachable,
        sentence=sentence,
    )


def build_card(
    scores: ScoreSet,
    name: str,
    *,
    threshold: float | None = None,
    method: str = "composite",
    alpha: float = 0.05,
    reps: int = 1000,
    seed: int = 0,
    budgets: Sequence[float] = CARD_BUDGETS,
    prevalences: Sequence[float] = CARD_PREVALENCES,
    notes: Sequence[str] = (),
) -> Card:
    """Compute a monitor report card.

    Args:
        scores: Labeled scores.
        name: The monitor's name, shown in the title.
        threshold: The monitor's own decision threshold (flag when score >=
            threshold); None leaves the decision-threshold section out.
        method: Interval method of the budget table.
        alpha: Miss probability of every interval and bound (0.05 for 95%).
        reps: Resamples for the resampling interval methods.
        seed: Seed of the resampling.
        budgets: Review budgets per 10,000 trajectories.
        prevalences: Deployment prevalences.
        notes: Extra lines for the Notes section (for example where the data
            came from).

    Returns:
        The card.
    """
    if not name.strip():
        raise ValueError("the card needs a non-empty name")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    if not budgets or not prevalences:
        raise ValueError("need at least one budget and one prevalence")
    data = summarize(scores, alpha)
    rows = budget_table(scores, prevalences, budgets, method, alpha, reps, seed)
    omitted: list[str] = []
    summary: ThresholdSummary | None = None
    gaps: tuple[CertificationGap, ...] = ()
    if scores.weights is not None:
        omitted.append(WEIGHTS_OMITTED)
    else:
        fp_runs: int | None = None
        if threshold is not None:
            summary = threshold_summary(scores, threshold, prevalences, alpha)
            fp_runs = summary.fp_clusters
        else:
            omitted.append(
                "No decision threshold was given, so the decision-threshold section is not "
                "computed and the certification gap assumes no false positives so far."
            )
        gaps = tuple(
            certification_gap(scores.n_neg_clusters, fp_runs, b, alpha)
            for b in budgets
            if 0 < flag_rate_from_budget(b) < 1
        )
    return Card(
        name=name.strip(),
        version=__version__,
        method=method,
        alpha=alpha,
        reps=reps,
        seed=seed,
        budgets=tuple(budgets),
        prevalences=tuple(prevalences),
        threshold_given=threshold,
        data=data,
        budget_rows=rows,
        e0_source=e0_support_source(),
        threshold=summary,
        certification=gaps,
        notes=tuple(notes),
        omitted=tuple(omitted),
    )


# ------------------------------------------------------------------ rendering


def e0_cell_text(warned: Sequence[str], method: str) -> str:
    """The E0 support entry of a budget-table row."""
    if not warned:
        return "no warning"
    return f"{method} under-covered in E0 cell {', '.join(warned)} (docs/UNSUPPORTED.md)"


def render_markdown(card: Card) -> str:
    """The card as Markdown."""
    d = card.data
    conf = confidence_text(card.alpha)
    out: list[str] = [f"# Monitor report card: {card.name}", ""]
    origin = f"`{d.source}`" if d.source else "scores given in code"
    digest = f" (SHA-256 `{d.sha256}`)" if d.sha256 else ""
    out += [
        f"Computed by baserate {card.version} from {origin}{digest}. Every number below is "
        f"computed from those scores. Intervals are {conf} and two-sided unless marked "
        "one-sided. Every bound outside the budget table counts independent clusters "
        "(source runs), not traces; the budget table says what its method counts.",
        "",
        "## Data",
        "",
        "| | Traces | Independent clusters |",
        "|---|---:|---:|",
        f"| Benign | {d.n_neg:,} | {d.n_neg_clusters:,} |",
        f"| Violation | {d.n_pos:,} | {d.n_pos_clusters:,} |",
        f"| All | {d.n_traces:,} | {d.n_clusters:,} |",
        "",
        "Clusters are counted per label; one cluster can hold traces of both labels (an "
        "inserted violation shares its benign run's cluster).",
        "",
    ]
    if d.n_synthetic is None or d.synthetic_share is None:
        out.append("- Synthetic share: not stated (the scores have no `synthetic` column).")
    else:
        out.append(
            f"- Synthetic share: {fmt_prob(d.synthetic_share)} ({d.n_synthetic:,} of "
            f"{d.n_traces:,} traces are marked synthetic)."
        )
    out.append(
        "- Sampling weights: present; the budget table is weighted."
        if d.weighted
        else "- Sampling weights: none."
    )
    if d.n_stage2 is not None:
        out.append(f"- Stage-2 scores: {d.n_stage2:,} of {d.n_traces:,} traces.")
    out.append(
        f"- With {_runs(d.n_neg_clusters)}, no false-positive rate below "
        f"{fmt_small(d.fpr_floor)} can be certified at {conf} (the one-sided bound when none "
        "of them has a false positive)."
    )
    out.extend(f"- From the score file: {c}" for c in d.comments if c)
    out += [
        "",
        "## Recall and precision at a review budget",
        "",
        "Humans review the top-scoring share of traffic that the budget allows. Recall is the "
        "share of violations reviewed, precision the share of reviews that are violations, "
        f"and FPR the share of benign traffic reviewed. Intervals: {card.method}, {conf} "
        f"two-sided (resamples {card.reps:,}, seed {card.seed}); {method_note(card.method)}.",
        "",
        "| Prevalence | Budget per 10,000 | Recall | Precision | FPR | E0 support |",
        "|---:|---:|---|---|---|---|",
    ]
    for r in card.budget_rows:
        out.append(
            f"| {r.prevalence:g} | {r.budget_per_10k:g} | {fmt_interval(r.recall)} | "
            f"{fmt_interval(r.precision)} | {fmt_interval(r.fpr, small=True)} | "
            f"{e0_cell_text(r.warnings, card.method)} |"
        )
    out += ["", f"E0 support data: {card.e0_source}.", ""]
    t = card.threshold
    if t is not None:
        out += [
            f"## Decision threshold: flag when score >= {t.threshold:g}",
            "",
            f"Intervals: {t.method} (exact bounds with clusters as the units), {conf} two-sided.",
            "",
            f"- False-positive rate: {fmt_interval(t.fpr, small=True)} ({t.flagged_neg:,} of "
            f"{d.n_neg:,} "
            f"benign traces flagged; {t.fp_clusters:,} of {t.n_neg_clusters:,} benign "
            "clusters have a flagged trace).",
            f"- One-sided {conf} upper bound on the false-positive rate, counting a benign "
            "cluster as a false positive if any of its traces is flagged: "
            f"{fmt_small(t.fpr_upper)}.",
            f"- Recall: {fmt_interval(t.recall)} ({t.flagged_pos:,} of {d.n_pos:,} violation "
            f"traces flagged; {t.detected_clusters:,} of {t.n_pos_clusters:,} violation "
            "clusters have every trace flagged).",
        ]
        out.extend(f"- Precision at prevalence {p:g}: {fmt_interval(iv)}." for p, iv in t.precision)
        out.append("")
    if card.certification:
        out += ["## Certification gap", ""]
        out.extend(f"- {g.sentence}" for g in card.certification)
        out.append("")
    notes = [*card.omitted, *card.notes, REPRESENTATIVE_NOTE]
    out += ["## Notes", ""]
    out.extend(f"- {n}" for n in notes)
    return "\n".join(out) + "\n"


def jsonable(value: Any) -> Any:
    """``value`` with every non-finite float replaced by None (valid JSON)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    return value


def render_json(card: Card) -> str:
    """The card as indented JSON (schema :data:`SCHEMA`)."""
    return json.dumps(jsonable(card.to_dict()), indent=2, allow_nan=False) + "\n"
