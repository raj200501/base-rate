"""Two-stage monitor cascades under a review budget.

A cheap stage-1 monitor scores every trajectory. Trajectories with a stage-1
score at or above ``tau1`` are escalated to an expensive stage-2 monitor, and
humans review the top ``flag_rate`` of all traffic by stage-2 score among the
escalated trajectories. Labeled data come from a case-control design, so each
class is reweighted to its deployment mass exactly as in
:mod:`baserate.estimate.operating`.

Definitions used throughout:

- The escalation rule is ``(tau1, tie)``: escalate every trace with a
  stage-1 score above ``tau1`` and a fraction ``tie`` of the mass exactly at
  it. For a target escalation rate ``e`` the rule is the randomized
  threshold of :func:`baserate.estimate.budget_point` at flag rate ``e`` on
  the stage-1 scores, so exactly ``e`` of deployment traffic escalates.
- Stage 2 flags, among escalated traces, the top ``flag_rate`` of all
  traffic by stage-2 score (randomized at ties).
- **Escalation below the flag rate.** When ``e < flag_rate`` there are fewer
  escalated traces than reviews. Every escalated trace is reviewed and the
  leftover reviews are not spent (stage 2 never sees the rest, so there is
  nothing ranked to review). Recall is then ``P(escalated | violation)``,
  the review rate is ``e``, and precision is ``prevalence * recall / e``.
- Expected cost per trajectory is ``c1 + e * c2``. Single-monitor baselines
  in the same units: stage 1 alone costs ``c1``; stage 2 alone on all
  traffic costs ``c2``.
- Stage-2 scores may be NaN for traces that stage 2 never scored, but only
  for traces that no considered rule escalates; an escalated trace with a
  missing stage-2 score raises ValueError.

:func:`sweep_cascade` evaluates a grid of escalation rates and the two
baselines and returns the cost-recall Pareto front;
:func:`best_under_budget` picks the best point under a compute budget;
:func:`split_by_run` and :func:`validate_holdout` choose the rule on a
training split and evaluate it on a test split, with a cluster bootstrap
interval for test recall.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray

from baserate.estimate.atbudget import Interval, Labels
from baserate.estimate.bootstrap import (
    Kind,
    chunks,
    cluster_index,
    cluster_weights,
    percentile_interval,
)
from baserate.estimate.operating import budget_point, budget_recall_batch, budget_recall_tail

FloatArray = NDArray[np.float64]
PointKind = Literal["cascade", "stage1", "stage2"]

_TOL = 1e-12
_MAX_CELLS = 1_000_000


@dataclass(frozen=True, eq=False)
class TwoStageScores:
    """Stage-1 and stage-2 scores of benign (``neg``) and violation (``pos``) traces.

    Build with :func:`two_stage_scores`, which validates the arrays.

    Attributes:
        s1_neg: Stage-1 scores of benign traces (finite).
        s2_neg: Stage-2 scores of benign traces (NaN where stage 2 did not run).
        s1_pos: Stage-1 scores of violation traces (finite).
        s2_pos: Stage-2 scores of violation traces (NaN where stage 2 did not run).
        neg_clusters: Source-run label per benign trace, or None (independent).
        pos_clusters: Source-run label per violation trace, or None.
    """

    s1_neg: FloatArray
    s2_neg: FloatArray
    s1_pos: FloatArray
    s2_pos: FloatArray
    neg_clusters: NDArray[Any] | None = None
    pos_clusters: NDArray[Any] | None = None

    @property
    def n_neg(self) -> int:
        """Number of benign traces."""
        return int(self.s1_neg.size)

    @property
    def n_pos(self) -> int:
        """Number of violation traces."""
        return int(self.s1_pos.size)

    def subset(self, neg_mask: ArrayLike, pos_mask: ArrayLike) -> TwoStageScores:
        """The traces selected by boolean masks over each class."""
        mn = np.asarray(neg_mask, dtype=bool)
        mp = np.asarray(pos_mask, dtype=bool)
        return two_stage_scores(
            self.s1_neg[mn],
            self.s2_neg[mn],
            self.s1_pos[mp],
            self.s2_pos[mp],
            None if self.neg_clusters is None else self.neg_clusters[mn],
            None if self.pos_clusters is None else self.pos_clusters[mp],
        )


def two_stage_scores(
    s1_neg: ArrayLike,
    s2_neg: ArrayLike,
    s1_pos: ArrayLike,
    s2_pos: ArrayLike,
    neg_clusters: Labels = None,
    pos_clusters: Labels = None,
) -> TwoStageScores:
    """Validate and bundle the scores of a two-stage cascade.

    Args:
        s1_neg: Stage-1 scores of benign traces.
        s2_neg: Stage-2 scores of the same benign traces (NaN allowed).
        s1_pos: Stage-1 scores of violation traces.
        s2_pos: Stage-2 scores of the same violation traces (NaN allowed).
        neg_clusters: Source-run label per benign trace (None: independent).
        pos_clusters: Source-run label per violation trace.

    Returns:
        The bundled scores.
    """
    arrs = [np.asarray(a, dtype=np.float64).ravel() for a in (s1_neg, s2_neg, s1_pos, s2_pos)]
    s1n, s2n, s1p, s2p = arrs
    if s1n.size == 0 or s1p.size == 0:
        raise ValueError("need at least one benign and one violation trace")
    if s1n.shape != s2n.shape or s1p.shape != s2p.shape:
        raise ValueError("stage-1 and stage-2 scores must align trace by trace")
    if not (np.all(np.isfinite(s1n)) and np.all(np.isfinite(s1p))):
        raise ValueError("stage-1 scores must be finite")
    if np.any(np.isinf(s2n)) or np.any(np.isinf(s2p)):
        raise ValueError("stage-2 scores must be finite or NaN")

    def labels(c: Labels, n: int) -> NDArray[Any] | None:
        if c is None:
            return None
        out = np.asarray(c)
        if out.shape != (n,):
            raise ValueError("need one cluster label per trace")
        return out

    return TwoStageScores(
        s1n, s2n, s1p, s2p, labels(neg_clusters, s1n.size), labels(pos_clusters, s1p.size)
    )


@dataclass(frozen=True)
class CascadePoint:
    """One cascade (or single-monitor baseline) at a review budget.

    Attributes:
        kind: ``"cascade"``, ``"stage1"`` (stage 1 alone), or ``"stage2"``
            (stage 2 alone on all traffic).
        escalation_rate: Fraction of deployment traffic sent to stage 2
            (0 for stage 1 alone, 1 for stage 2 alone).
        threshold: For a cascade, the stage-1 escalation threshold ``tau1``;
            for a baseline, that monitor's review threshold.
        tie_fraction: Fraction of the mass at ``threshold`` that escalates
            (or, for a baseline, that is reviewed).
        cost: Expected monitor cost per trajectory.
        recall: P(reviewed | violation) at the deployment prevalence.
        precision: P(violation | reviewed).
        fpr: P(reviewed | benign).
        review_rate: Fraction of traffic reviewed: ``flag_rate``, or ``e``
            when fewer than ``flag_rate`` escalate.
    """

    kind: PointKind
    escalation_rate: float
    threshold: float
    tie_fraction: float
    cost: float
    recall: float
    precision: float
    fpr: float
    review_rate: float


@dataclass(frozen=True)
class CascadeSweep:
    """Cascades over a grid of escalation rates, the baselines, and the Pareto front.

    Attributes:
        candidates: Cascades in ascending escalation rate.
        stage1: Stage 1 alone at the review budget.
        stage2: Stage 2 alone on all traffic, or None when some stage-2
            scores are missing.
        front: Non-dominated points (lower cost, higher recall) among the
            candidates and baselines, in ascending cost.
        prevalence: Deployment prevalence.
        flag_rate: Fraction of traffic reviewed.
        c1: Stage-1 cost per trajectory.
        c2: Stage-2 cost per escalated trajectory.
    """

    candidates: tuple[CascadePoint, ...]
    stage1: CascadePoint
    stage2: CascadePoint | None
    front: tuple[CascadePoint, ...]
    prevalence: float
    flag_rate: float
    c1: float
    c2: float

    @property
    def points(self) -> tuple[CascadePoint, ...]:
        """Candidates followed by the available baselines."""
        base = (self.stage1,) if self.stage2 is None else (self.stage1, self.stage2)
        return self.candidates + base

    def best_under(self, max_cost: float) -> CascadePoint | None:
        """Highest-recall point (cascade or baseline) with cost at most ``max_cost``."""
        return best_under_budget(self.points, max_cost)


def default_escalation_rates(flag_rate: float, num: int = 25) -> FloatArray:
    """Geometric grid of escalation rates from ``flag_rate`` to 1.

    Rates below the flag rate are left out: such a cascade is dominated by
    stage 1 alone (higher cost, and recall ``P(escalated | violation)`` at
    most stage 1's recall at the budget).
    """
    if not 0.0 < flag_rate <= 1.0:
        raise ValueError("flag_rate must be in (0, 1]")
    if num < 1:
        raise ValueError("num must be at least 1")
    if flag_rate == 1.0 or num == 1:
        return np.array([1.0])
    rates = np.geomspace(flag_rate, 1.0, num)
    rates[-1] = 1.0
    return np.asarray(rates, dtype=np.float64)


def _filled(s2: FloatArray, floor: float) -> FloatArray:
    return np.where(np.isnan(s2), floor, s2)


def _s2_floor(sc: TwoStageScores) -> float:
    finite = np.r_[sc.s2_neg[~np.isnan(sc.s2_neg)], sc.s2_pos[~np.isnan(sc.s2_pos)]]
    return float(finite.min()) - 1.0 if finite.size else 0.0


def _evaluate(
    sc: TwoStageScores,
    prevalence: float,
    flag_rate: float,
    threshold: FloatArray,
    tie: FloatArray,
    neg_w: FloatArray,
    pos_w: FloatArray,
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Escalation rate, recall, and review rate for R rules (rows) under R weightings.

    ``neg_w`` and ``pos_w`` are (R, n) or (1, n) non-negative class weights.
    """
    r = threshold.size
    floor = _s2_floor(sc)
    s2n, s2p = _filled(sc.s2_neg, floor), _filled(sc.s2_pos, floor)
    miss_n, miss_p = np.isnan(sc.s2_neg), np.isnan(sc.s2_pos)
    e_out, rec_out, rev_out = np.empty(r), np.empty(r), np.empty(r)
    done = 0
    for size in chunks(r, sc.n_neg + sc.n_pos, _MAX_CELLS):
        rows = slice(done, done + size)
        thr, tf = threshold[rows, None], tie[rows, None]
        esc_n = (sc.s1_neg > thr) + tf * (sc.s1_neg == thr)
        esc_p = (sc.s1_pos > thr) + tf * (sc.s1_pos == thr)
        missing = int(np.sum(np.any(esc_n > 0, axis=0) & miss_n)) + int(
            np.sum(np.any(esc_p > 0, axis=0) & miss_p)
        )
        if missing:
            raise ValueError(
                f"{missing} escalated traces have no stage-2 score; stage 2 must score "
                "every trace at or above the lowest escalation threshold used"
            )
        wn = neg_w if neg_w.shape[0] == 1 else neg_w[rows]
        wp = pos_w if pos_w.shape[0] == 1 else pos_w[rows]
        gn, gp = esc_n * wn, esc_p * wp
        tot_n = np.broadcast_to(wn.sum(axis=1), (size,))
        tot_p = np.broadcast_to(wp.sum(axis=1), (size,))
        mass_n, mass_p = gn.sum(axis=1) / tot_n, gp.sum(axis=1) / tot_p
        e = (1 - prevalence) * mass_n + prevalence * mass_p
        rec_tail, ok = budget_recall_tail(s2n, s2p, prevalence, flag_rate, gn, gp, tot_n, tot_p)
        # fewer escalations than reviews: review every escalated trace, spend no more
        rec_out[rows] = np.where(ok, rec_tail, mass_p)
        rev_out[rows] = np.where(ok, flag_rate, e)
        e_out[rows] = e
        done += size
    return e_out, np.clip(rec_out, 0.0, 1.0), rev_out


def _point(
    kind: PointKind,
    e: float,
    threshold: float,
    tie: float,
    cost: float,
    recall: float,
    review: float,
    prevalence: float,
) -> CascadePoint:
    precision = min(prevalence * recall / review, 1.0) if review > 0 else 0.0
    fpr = max((review - prevalence * recall) / (1 - prevalence), 0.0)
    return CascadePoint(kind, e, threshold, tie, cost, recall, precision, fpr, review)


def _check_costs(c1: float, c2: float) -> None:
    if c1 < 0 or c2 < 0:
        raise ValueError("costs must be non-negative")


def escalation_rule(
    scores: TwoStageScores, prevalence: float, escalation_rate: float
) -> tuple[float, float]:
    """The stage-1 rule ``(tau1, tie)`` that escalates exactly ``escalation_rate`` of traffic.

    Args:
        scores: Cascade scores.
        prevalence: Deployment prevalence.
        escalation_rate: Target fraction of traffic escalated, in (0, 1].

    Returns:
        (threshold, tie_fraction).
    """
    op = budget_point(scores.s1_neg, scores.s1_pos, prevalence, escalation_rate)
    return op.threshold, op.tie_fraction


def evaluate_rule(
    scores: TwoStageScores,
    prevalence: float,
    flag_rate: float,
    threshold: float,
    tie_fraction: float,
    c1: float,
    c2: float,
) -> CascadePoint:
    """Evaluate a fixed escalation rule (for example one chosen on other data).

    The escalation rate follows from the data; stage 2 then reviews the top
    ``flag_rate`` of traffic among escalated traces (all of them if fewer).

    Args:
        scores: Cascade scores.
        prevalence: Deployment prevalence, in (0, 1).
        flag_rate: Fraction of traffic reviewed, in (0, 1].
        threshold: Stage-1 escalation threshold ``tau1``.
        tie_fraction: Fraction of the mass at ``tau1`` that escalates, in [0, 1].
        c1: Stage-1 cost per trajectory.
        c2: Stage-2 cost per escalated trajectory.

    Returns:
        The cascade point.
    """
    _check_costs(c1, c2)
    if not 0.0 <= tie_fraction <= 1.0:
        raise ValueError("tie_fraction must be in [0, 1]")
    e, rec, rev = _evaluate(
        scores,
        prevalence,
        flag_rate,
        np.array([threshold], dtype=np.float64),
        np.array([tie_fraction], dtype=np.float64),
        np.ones((1, scores.n_neg)),
        np.ones((1, scores.n_pos)),
    )
    return _point(
        "cascade",
        float(e[0]),
        threshold,
        tie_fraction,
        c1 + float(e[0]) * c2,
        float(rec[0]),
        float(rev[0]),
        prevalence,
    )


def _rates(escalation_rates: ArrayLike | None, flag_rate: float) -> FloatArray:
    if escalation_rates is None:
        return default_escalation_rates(flag_rate)
    rates = np.unique(np.asarray(escalation_rates, dtype=np.float64).ravel())
    if rates.size == 0 or rates[0] <= 0.0 or rates[-1] > 1.0:
        raise ValueError("escalation rates must lie in (0, 1]")
    return rates


def _rules(
    scores: TwoStageScores, prevalence: float, rates: FloatArray
) -> tuple[FloatArray, FloatArray]:
    found = [escalation_rule(scores, prevalence, float(e)) for e in rates]
    return (
        np.array([f[0] for f in found], dtype=np.float64),
        np.array([f[1] for f in found], dtype=np.float64),
    )


def sweep_cascade(
    scores: TwoStageScores,
    prevalence: float,
    flag_rate: float,
    c1: float,
    c2: float,
    escalation_rates: ArrayLike | None = None,
) -> CascadeSweep:
    """Cost, recall, and precision of cascades over a grid of escalation rates.

    Args:
        scores: Cascade scores.
        prevalence: Deployment prevalence, in (0, 1).
        flag_rate: Fraction of traffic reviewed, in (0, 1].
        c1: Stage-1 cost per trajectory.
        c2: Stage-2 cost per escalated trajectory.
        escalation_rates: Rates in (0, 1]; default
            :func:`default_escalation_rates`.

    Returns:
        Candidates, baselines, and the Pareto front.
    """
    _check_costs(c1, c2)
    rates = _rates(escalation_rates, flag_rate)
    thr, tie = _rules(scores, prevalence, rates)
    e, rec, rev = _evaluate(
        scores,
        prevalence,
        flag_rate,
        thr,
        tie,
        np.ones((1, scores.n_neg)),
        np.ones((1, scores.n_pos)),
    )
    candidates = tuple(
        _point(
            "cascade",
            float(e[i]),
            float(thr[i]),
            float(tie[i]),
            c1 + float(e[i]) * c2,
            float(rec[i]),
            float(rev[i]),
            prevalence,
        )
        for i in range(rates.size)
    )
    op1 = budget_point(scores.s1_neg, scores.s1_pos, prevalence, flag_rate)
    stage1 = _point(
        "stage1", 0.0, op1.threshold, op1.tie_fraction, c1, op1.recall, flag_rate, prevalence
    )
    stage2 = None
    if not (np.isnan(scores.s2_neg).any() or np.isnan(scores.s2_pos).any()):
        op2 = budget_point(scores.s2_neg, scores.s2_pos, prevalence, flag_rate)
        stage2 = _point(
            "stage2", 1.0, op2.threshold, op2.tie_fraction, c2, op2.recall, flag_rate, prevalence
        )
    base = (stage1,) if stage2 is None else (stage1, stage2)
    return CascadeSweep(
        candidates=candidates,
        stage1=stage1,
        stage2=stage2,
        front=pareto_front(candidates + base),
        prevalence=prevalence,
        flag_rate=flag_rate,
        c1=c1,
        c2=c2,
    )


def pareto_front(points: Sequence[CascadePoint]) -> tuple[CascadePoint, ...]:
    """Points not dominated in (lower cost, higher recall), in ascending cost.

    A point is dropped when another costs no more and recalls at least as
    much (exact duplicates keep the first).
    """
    ordered = sorted(points, key=lambda p: (p.cost, -p.recall))
    front: list[CascadePoint] = []
    best = -np.inf
    for p in ordered:
        if p.recall > best + _TOL:
            front.append(p)
            best = p.recall
    return tuple(front)


def best_under_budget(points: Sequence[CascadePoint], max_cost: float) -> CascadePoint | None:
    """Highest-recall point with cost at most ``max_cost`` (ties: lower cost); None if none."""
    feasible = [p for p in points if p.cost <= max_cost + _TOL]
    if not feasible:
        return None
    return max(feasible, key=lambda p: (p.recall, -p.cost))


def _keys(labels: NDArray[Any] | None, n: int, tag: str) -> NDArray[np.str_]:
    if labels is None:
        return np.char.add(f"\x00{tag}:", np.arange(n).astype(str))
    return np.asarray(labels).astype(str)


def split_by_run(
    scores: TwoStageScores, test_fraction: float = 0.5, seed: int = 0
) -> tuple[TwoStageScores, TwoStageScores]:
    """Split traces into training and test sets, keeping every source run on one side.

    Source runs are matched across classes by label, so a benign run and the
    violation traces inserted into it stay together. Runs with any violation
    trace and benign-only runs are each split at ``test_fraction``
    (stratified), so both sides get violations.

    Args:
        scores: Cascade scores with cluster labels (None: each trace its own run).
        test_fraction: Share of runs in the test split, in (0, 1).
        seed: Seed of the random assignment.

    Returns:
        (train, test).
    """
    if not 0.0 < test_fraction < 1.0:
        raise ValueError("test_fraction must be in (0, 1)")
    kn = _keys(scores.neg_clusters, scores.n_neg, "neg")
    kp = _keys(scores.pos_clusters, scores.n_pos, "pos")
    runs = np.unique(np.r_[kn, kp])
    has_pos = np.isin(runs, kp)
    rng = np.random.default_rng(seed)
    test_runs: list[NDArray[np.str_]] = []
    for group in (runs[has_pos], runs[~has_pos]):
        if group.size == 0:
            continue
        k = round(test_fraction * group.size)
        if group.size >= 2:
            k = min(max(k, 1), group.size - 1)
        test_runs.append(rng.permutation(group)[:k])
    chosen = np.concatenate(test_runs) if test_runs else np.array([], dtype=str)
    tn, tp = np.isin(kn, chosen), np.isin(kp, chosen)
    if tn.all() or not tn.any() or tp.all() or not tp.any():
        raise ValueError("too few source runs to put both classes on both sides of the split")
    return scores.subset(~tn, ~tp), scores.subset(tn, tp)


@dataclass(frozen=True)
class HoldoutResult:
    """A cascade rule chosen on training data and evaluated on test data.

    Attributes:
        chosen: The rule's point on the training split.
        test: The same rule ``(tau1, tie)`` on the test split; its escalation
            rate (hence cost) is re-estimated there and stage 2 reviews the
            top ``flag_rate`` of test traffic among escalated traces.
        recall: Test recall with a cluster bootstrap interval that resamples
            source runs of both splits and both classes and repeats the
            selection on every resampled training set.
        cost: Test expected cost per trajectory with the same bootstrap.
        max_cost: Compute budget used for the selection.
        n_train_neg: Benign traces in training.
        n_train_pos: Violation traces in training.
        n_test_neg: Benign traces in test.
        n_test_pos: Violation traces in test.
        n_train_neg_runs: Benign source runs in training.
        n_train_pos_runs: Violation source runs in training.
        n_test_neg_runs: Benign source runs in test.
        n_test_pos_runs: Violation source runs in test.
        reps: Bootstrap replicates.
    """

    chosen: CascadePoint
    test: CascadePoint
    recall: Interval
    cost: Interval
    max_cost: float
    n_train_neg: int
    n_train_pos: int
    n_test_neg: int
    n_test_pos: int
    n_train_neg_runs: int
    n_train_pos_runs: int
    n_test_neg_runs: int
    n_test_pos_runs: int
    reps: int


def _select(points: Sequence[CascadePoint], max_cost: float) -> int:
    """Index of the highest-recall cascade within budget (ties: lowest escalation)."""
    best, best_recall = -1, -np.inf
    for i, p in enumerate(points):
        if p.cost <= max_cost + _TOL and p.recall > best_recall + _TOL:
            best, best_recall = i, p.recall
    return best


def validate_holdout(
    train: TwoStageScores,
    test: TwoStageScores,
    prevalence: float,
    flag_rate: float,
    c1: float,
    c2: float,
    max_cost: float,
    *,
    escalation_rates: ArrayLike | None = None,
    alpha: float = 0.05,
    reps: int = 200,
    seed: int = 0,
    kind: Kind = "multinomial",
) -> HoldoutResult:
    """Choose the escalation rule on ``train`` under a compute budget and test it on ``test``.

    Selection: the cascade with the highest training recall among those with
    cost ``c1 + e * c2 <= max_cost`` (ties go to the lower escalation rate).
    The chosen rule ``(tau1, tie)`` is applied unchanged to the test split.

    Interval: each bootstrap replicate resamples source runs within each
    class of each split (:func:`baserate.estimate.bootstrap.cluster_weights`),
    repeats the selection on the resampled training set, and evaluates the
    replicate's rule on the resampled test set. The percentile interval is
    widened to contain the point estimate if needed. Stage-2 scores must
    exist for every trace that a replicate's rule escalates.

    Args:
        train: Training scores (see :func:`split_by_run`).
        test: Test scores.
        prevalence: Deployment prevalence.
        flag_rate: Fraction of traffic reviewed.
        c1: Stage-1 cost per trajectory.
        c2: Stage-2 cost per escalated trajectory.
        max_cost: Compute budget per trajectory.
        escalation_rates: Candidate rates; default :func:`default_escalation_rates`.
        alpha: Miss probability of the two-sided intervals.
        reps: Bootstrap replicates.
        seed: Seed; the result is deterministic given it.
        kind: ``"multinomial"`` (cluster bootstrap) or ``"bayesian"``.

    Returns:
        The chosen rule on both splits with test intervals.
    """
    if reps < 1:
        raise ValueError("reps must be at least 1")
    sweep = sweep_cascade(train, prevalence, flag_rate, c1, c2, escalation_rates)
    pick = _select(sweep.candidates, max_cost)
    if pick < 0:
        raise ValueError("no cascade on the grid fits within max_cost")
    chosen = sweep.candidates[pick]
    test_point = evaluate_rule(
        test, prevalence, flag_rate, chosen.threshold, chosen.tie_fraction, c1, c2
    )
    rates = _rates(escalation_rates, flag_rate)
    feasible = np.flatnonzero(c1 + rates * c2 <= max_cost + _TOL)
    rng = np.random.default_rng(seed)
    idx = [
        cluster_index(train.neg_clusters, train.n_neg),
        cluster_index(train.pos_clusters, train.n_pos),
        cluster_index(test.neg_clusters, test.n_neg),
        cluster_index(test.pos_clusters, test.n_pos),
    ]
    rec_s: list[FloatArray] = []
    cost_s: list[FloatArray] = []
    total = train.n_neg + train.n_pos + test.n_neg + test.n_pos
    for size in chunks(reps, total, _MAX_CELLS):
        wtn, wtp, wsn, wsp = (cluster_weights(i, size, rng, kind) for i in idx)
        recalls = np.empty((feasible.size, size))
        thrs = np.empty((feasible.size, size))
        ties = np.empty((feasible.size, size))
        for j, k in enumerate(feasible):
            _, thr, tie = budget_recall_batch(
                train.s1_neg, train.s1_pos, prevalence, float(rates[k]), wtn, wtp
            )
            _, rec, _ = _evaluate(train, prevalence, flag_rate, thr, tie, wtn, wtp)
            recalls[j], thrs[j], ties[j] = rec, thr, tie
        # first maximum: the lowest escalation rate among ties
        best = np.argmax(recalls >= recalls.max(axis=0) - _TOL, axis=0)
        cols = np.arange(size)
        e_t, rec_t, _ = _evaluate(
            test, prevalence, flag_rate, thrs[best, cols], ties[best, cols], wsn, wsp
        )
        rec_s.append(rec_t)
        cost_s.append(c1 + e_t * c2)
    rec_lo, rec_hi = percentile_interval(np.concatenate(rec_s), alpha)
    cost_lo, cost_hi = percentile_interval(np.concatenate(cost_s), alpha)
    method = f"holdout_{'cluster' if kind == 'multinomial' else 'bayesian'}_bootstrap"
    r, c = test_point.recall, test_point.cost
    return HoldoutResult(
        chosen=chosen,
        test=test_point,
        recall=Interval(r, min(rec_lo, r), max(rec_hi, r), method, alpha),
        cost=Interval(c, min(cost_lo, c), max(cost_hi, c), method, alpha),
        max_cost=max_cost,
        n_train_neg=train.n_neg,
        n_train_pos=train.n_pos,
        n_test_neg=test.n_neg,
        n_test_pos=test.n_pos,
        n_train_neg_runs=int(idx[0].max()) + 1,
        n_train_pos_runs=int(idx[1].max()) + 1,
        n_test_neg_runs=int(idx[2].max()) + 1,
        n_test_pos_runs=int(idx[3].max()) + 1,
        reps=reps,
    )
