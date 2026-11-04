# base-rate: what an AI agent monitor is worth at realistic prevalence

Technical report. Every result number in this document is rendered from a banked
artifact under `artifacts/` by `make report`; parameters, dates, and labels carry
explicit allowance tags in the source (docs/templates/REPORT.prose.md). Synthetic
and procedurally generated data are labeled as such wherever they appear.

<!-- @section abstract -->

## Abstract

<!-- @prose abstract -->

## 1. Introduction

Labs now run monitors over the traffic of their tool-using agents. The question a
safety case needs answered about such a monitor is not its area under the curve on a
balanced test set. It is closer to this: if one trajectory in ten thousand contains a
real policy violation, and humans can review a fixed number of trajectories per ten
thousand, what fraction of violations gets caught, how quickly, at what compute cost,
and with what confidence? Answering it needs three things that balanced evaluations do
not supply: estimates at the deployment prevalence, estimates at the review budget the
team can actually staff, and intervals that are honest about how few independent
benign runs stand behind a false-positive rate.

This report describes base-rate, an open library and study for that question. The
library estimates recall, precision, and the false-positive rate at a fixed review
budget and a stated prevalence, with interval methods whose coverage was checked by a
preregistered simulation; computes how many labeled runs a claim needs; tests whether a
threshold tuned in one setting transfers to another; and optimizes cheap-to-expensive
monitor cascades under compute and review budgets. The study applies it to a labeled
corpus of agent trajectories in mock-tool domains, built for this purpose, and to four
monitor families: rule monitors, a size ladder of small open-weight model monitors with
one frozen prompt, an ensemble, and a cascade.

The contributions are: (1) estimators and interval methods for recall and precision at
a review budget under case-control sampling and source-run clustering, with a
preregistered coverage study (E0); (2) a labeled corpus with step-level labels for five
abstract violation types, separating natural, inserted, and dataset violations (E1);
(3) a preregistered main study of the monitor families at three prevalences and three
review budgets (E2); (4) preregistered tests of threshold transfer (E3) and of cascade
selection on held-out data (E4); and (5) sample-size calculators that state how many more
independent runs a claim would need (RQ2).

## 2. Related work

<!-- @prose related -->

## 3. Method

### 3.1. Setting and estimands

A monitor assigns each trajectory a suspicion score. In deployment, a fraction
$\pi$ (the prevalence) of trajectories are violations, and humans review a fixed
fraction $q$ of all trajectories, written as a budget of reviews per ten thousand (for
example {{param:20}} per ten thousand is $q = $ {{param:0.002}}). The reviewed set is the
top $q$ of traffic by score. Labeled evaluation data are collected case-control: benign
and violation trajectories are sampled separately, so each class is reweighted to its
deployment mass (the benign share and the prevalence) before the threshold is placed. Because coarse
monitors (rules) tie heavily, the threshold is randomized at ties: everything above it
is reviewed, plus a fraction of the mass exactly at it, so the review rate is met
exactly. At that operating point, recall is the probability that a violation is
reviewed, precision is $\pi \cdot \text{recall} / q$, and the false-positive rate is
$(q - \pi \cdot \text{recall}) / ({{param:1}} - \pi)$. At a fixed review budget all three are
monotone functions of recall, so one interval for recall gives the other two.

A second operating mode is the monitor's own decision threshold (a rule fires or not).
There, recall and the false-positive rate are binomial proportions, and precision at
prevalence $\pi$ is $\pi R / (\pi R + ({{param:1}} - \pi) F)$.

Trajectories are not independent. Procedural variants of one agent run, and the
violation inserted into a benign run, share that run's source-run id. Every interval in
base-rate resamples or counts at the level of source runs, and every table states the
number of independent source runs beside the trace count.

### 3.2. Interval methods

For recall at a budget, the library offers seven methods. Three are plug-in binomial
intervals on the violation sample at the estimated threshold (Clopper and Pearson
[@clopper-pearson-1934], Wilson [@wilson-1927], and Jeffreys [@brown-cai-dasgupta-2001]);
they ignore the uncertainty of the threshold itself, which comes from the benign tail.
One is Clopper-Pearson with the number of violation source runs as the sample size. Two
resample source runs within each class: the cluster bootstrap and the Bayesian bootstrap
[@rubin-1981] at cluster level. The last, called composite, takes threshold uncertainty
from resampling benign source runs and combines it with exact Clopper-Pearson bounds on
the violation sample at the extreme thresholds, spending a quarter of the miss
probability on each of the four tails.

At a fixed threshold, the cluster-aware exact method bounds the false-positive rate from
above with Clopper-Pearson on the source runs that contain any flagged trace, and bounds
recall from below with the runs whose traces are all flagged. With zero false positives
in $n$ independent runs, the exact one-sided upper bound at {{param:95%}} is
{{param:1 - 0.05^(1/n)}}, close to {{param:3/n}} (the rule of three [@hanley-lippman-hand-1983]). No
false-positive rate in this report is stated more tightly than the independent benign
runs behind it allow.

Recall at a budget depends only on the score tail around the threshold. The resampling
methods therefore draw individual weights only for source runs with a trace in that
tail and one aggregate weight for the rest, which is exact for both the multinomial and
the Dirichlet resampling when the other runs have equal weight, and widen the tail until
every replicate's threshold falls inside it. Interval methods were adopted for headline
use only where the preregistered simulation (E0) supports them; calls in a configuration
where E0 found under-coverage emit a warning, and weighted (importance-sampled) designs,
which E0 did not cover, always warn.

### 3.3. Certification and planning

To certify a false-positive rate of at most $f$ at confidence ${{param:1}} - \alpha$ with no false
positives, $n = \lceil \ln \alpha / \ln({{param:1}} - f) \rceil$ independent benign runs are needed;
the same form with recall gives the violations needed to certify recall of at least $r$
with no misses. With assumed true rates and a power target, the calculators search the
exact binomial distribution for the smallest $n$ after which the probability of
certifying stays above the target (the sawtooth of discrete tests means a single $n$ can
pass while the next larger $n$ fails). For an existing claim with $k$ events in $n$ runs, they state
how many more runs the claim needs if no further event occurs, and at the observed rate.
At a review budget, planning is by simulation from pilot scores.

### 3.4. Cascades

A two-stage cascade scores all traffic with a cheap monitor, escalates the top share of
traffic (under the same case-control reweighting) to an expensive monitor, and reviews
the top of the budget by the expensive score among escalated trajectories. Leftover
reviews are not spent when fewer trajectories escalate than the budget allows. The
expected compute per trajectory is $c_1 + e \cdot c_2$ for escalation rate $e$. The
library sweeps escalation rates, returns the cost-recall Pareto front with single-monitor
baselines, and validates a cascade chosen on a training split of source runs on the test
split, with a bootstrap that repeats the selection on every resampled training set.

### 3.5. Corpus

<!-- @prose corpus-method -->

### 3.6. Monitors

<!-- @prose monitors-method -->

## 4. Experiments

<!-- @prose experiments -->

## 5. Results

<!-- @prose results -->

## 6. Limitations and threats to validity

<!-- @prose limitations -->

## 7. Ethics and disclosure

base-rate measures monitors; it does not attack them. Every violation in the corpus is
an abstract action in a simulated tool (for example a mock export of a customer table or
a mock deletion of an audit-log entry). No real credential, system, network, or payload
is involved, and the subject agents act only through in-process simulated tools: their
replies are parsed as structured tool calls and never executed. No part of the project
optimizes agents or trajectories to evade monitors. The local models used are
open-weight models under the Apache {{version:2.0}} license; the Armour records are MIT
licensed. No Claude model was used as a subject or a monitor in this environment, because
the session's billing guard found an API-routing setting (DECISIONS.md D2); Claude was
used only as an independent label auditor and as the project's reviewer. There is no
vulnerability to disclose: the study found no way to bypass a deployed monitor, and it
reports weaknesses of the evaluated monitors only as rates.

## 8. Reproducibility

`make reproduce` rebuilds every figure, table, and number in this report from the banked
artifacts, with no model calls. The full pipeline (corpus generation, scoring, and
analysis) is scripted in tools/ and pinned: model files by revision and hash
(configs/models.json), the llama.cpp build by tag and commit, sampling settings and
prompts by hash in each preregistration, and every random seed in the preregistrations.
The preregistrations under prereg/ were committed before the data they govern, and
`make prereg-verify` checks that chain. `make ship-check` runs the project's
definition-of-done gates on a fresh clone.

## References

<!-- @prose references -->
