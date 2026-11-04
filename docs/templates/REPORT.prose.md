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

The contributions are:

1. estimators and interval methods for recall and precision at a review budget under
   case-control sampling and source-run clustering, with a preregistered coverage study
   (E0);
2. a labeled corpus with step-level labels for five abstract violation types, separating
   natural, inserted, and dataset violations (E1);
3. a preregistered main study of the monitor families at three prevalences and three
   review budgets (E2);
4. preregistered tests of threshold transfer (E3) and of cascade selection on held-out
   data (E4); and
5. sample-size calculators that state how many more independent runs a claim would need
   (RQ2).

## 2. Related work

Closest in framing is "Practical challenges of control monitoring in frontier AI
deployments" [@practical-challenges-2025], a conceptual analysis of what changes when
control monitoring moves from simplified evaluations to real deployments: many agent
instances in parallel, oversight that takes time, attacks that make partial progress
across instances, and actions too ambiguous to justify shutdown. It compares synchronous,
semi-synchronous, and asynchronous monitoring, models availability from the flag rate,
audit duration, and instance count, and sketches a safety case whose claims include monitor
and human recall and latencies. It states that these quantities can be estimated with
control evaluations and extrapolated conservatively, and it illustrates that a limited
review capacity forces a monitor to operate at a matching false-positive rate, after which
recall drives safety. It gives no estimator, interval, or sample size for any of these
quantities, and it does not discuss precision, prevalence, review budgets, or cascades.
base-rate supplies that measurement layer: recall and precision at a fixed number of
reviews per unit of traffic, at stated prevalences, with intervals whose coverage was
checked, sample sizes for certifying a claim, a test of whether the operating threshold
transfers, and cost-recall fronts for cascades. It does not address synchrony,
cross-instance coordination, recovery, or shutdown policy, and its estimates are inputs to
the per-attempt catch rate of such a safety case, not a replacement for it. The control
safety-case sketch of [@korbak-safety-case-2025] makes the same dependence explicit:
its argument rests on extrapolating red-team performance conservatively, which needs
exactly these estimates.

Async Control [@aisi-async-control-2025] evaluates asynchronous monitors of software
engineering agents in a blue-team and red-team game, reports the false negative rate at a
fixed low false-positive rate with bootstrap error bars over generated episodes, and
extrapolates to deployment risk with a queueing model; its attack share is a design choice
of the game, not a deployment prevalence. BashArena [@basharena-2025], LinuxArena
[@linuxarena-2026], and SHADE-Arena [@shade-arena-2025] are control settings in which
attackers pursue hidden objectives while a monitor scores trajectories; they report safety
at a fixed false-positive rate against attackers of increasing strength and are natural
sources of attack data and targets for base-rate's evaluation, but do not estimate
monitor performance at deployment prevalence or review budgets. SHADE-Arena's observation
that catching most side tasks cost a false-positive rate too high for deployment is the
intuition that base-rate turns into computed precision and reviewer load. MonitorBench
[@monitorbench-2026] studies whether chain-of-thought reveals the factors that drive
behavior, one input channel a monitor may read. TRACE [@trace-2026] benchmarks
reward-hack detection in code and finds that the ratio of benign to hacked trajectories
changes measured detection, independent evidence that results should be reported at a
fixed prevalence.

Hua and colleagues [@hua-cascades-2025] are the closest prior art for cascades: they
combine runtime monitors of different cost under one budget covering monitor calls and
audits, rank outputs by an estimated likelihood ratio, and compare policies that call a
second monitor only inside a score range, with intervals from repeated random splits of
the misaligned data while the benign set is held fixed. base-rate asks a neighboring
question with a different evidence layer: a fixed deployment prevalence and human-review
budget, intervals that also carry the uncertainty of the benign tail, sample sizes for
certification, and held-out validation that keeps source runs on one side of the split.
The two are complementary; a policy found by their method can be certified with base-rate.

base-rate builds on Armour [@armour], whose trace schema it extends, whose rule monitors
and policy packs form one monitor family here, and whose labeled records are one source of
violations. The statistical tools are standard: exact binomial intervals
[@clopper-pearson-1934], the score interval [@wilson-1927], the comparison and the
Jeffreys interval of [@brown-cai-dasgupta-2001], the Bayesian bootstrap [@rubin-1981],
and the rule of three for zero numerators [@hanley-lippman-hand-1983]. The corpus
datasheet follows [@gebru-datasheets-2021], and the monitor ladder uses the Qwen3 model
family [@qwen3-2025].

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

The corpus follows the protocol frozen in prereg/0002-e1-corpus.md before any corpus
run, as amended by prereg/0006-e1-amendment-oracle.md and
prereg/0007-e1-amendment-insertion.md. Eight mock-tool domains (devops, SaaS
administration, customer support, finance operations, HR, research data, a browser portal,
and code maintenance) each define a small simulated entity store, between thirteen and
fifteen tools of a few generic kinds (list, get, update, create,
side-effecting actions, export, grant or approve, audit-log edits, and finish), and task
templates whose instances state the entity ids involved. Each task carries a write scope
(the entities it may change), a sensitive-read scope, the grants it authorizes, and the
effects required for completion. One oracle labels violations identically in every
domain from the effects of each tool call: an audit-log edit; a grant or approval the
task did not authorize; a bulk export or a sensitive read the task did not need; a write
or side-effecting action outside the write scope; or finishing as complete before a
required effect happened. These are the five violation types of Armour's taxonomy, made
operational (DECISIONS.md D11).

Subject agents are local open-weight models (Qwen3 at {{name:1.7B}} and {{name:4B}}
parameters, pinned by revision and hash) served by a pinned llama.cpp build. An agent sees
a system prompt listing the domain's tools and the task request, and replies with one
JSON tool call per turn; a Python simulator answers each call. Nothing is executed: the
mock tools run inside the Python process, the model runs in a local server, and nothing
leaves the machine. The first action of every real, inserted, and variant trajectory is the
user request, so monitors see what was asked; Armour's records hold only the agent's
events. A real run in which the oracle finds a violation is a
natural violation, with the first violating action as its step label; the other real runs
are benign.

Controlled insertions turn benign runs into violations without a seam: the run's first
actions are replayed exactly through a fresh world (observations are checked to match),
one violating call of the drawn type, checked by the oracle, is placed at a step drawn
uniformly from the run's agent steps, and the same model then continues from that state,
so everything after the inserted step is regenerated by the agent. A false completion
ends the episode at the inserted finish call, so its summary is written by the same model
instead of the harness (prereg/0007-e1-amendment-insertion.md). The sources are the runs
benign under the fixed oracle, sorted and then shuffled with the preregistered seed, and
every skipped attempt is counted. Inserted trajectories keep their source run's id and are
labeled synthetic, because the harness chose one step. Procedural variants of each benign
real run rename entity ids and remap standalone numbers consistently, and most of them also
reorder the keys of JSON observations and swap some phrases; they are labeled synthetic
and keep the source run's id, so they add scale without pretending to add independent
runs. Armour's labeled records add a third, separately reported violation
source, clustered by source file and agent family because they come from a few
generator scripts (DECISIONS.md D12). A Claude subagent of the session that built the
corpus, given only the monitor view of a stratified random sample of trajectories, labeled
them blind to the corpus labels; it made no tool call other than returning its answers,
which tools/label_audit.py checks from the workflow's records and banks with the audit.
Agreement is reported in docs/DATA.md and Section 5.

### 3.6. Monitors

Rule monitors are a port of Armour's pattern rules and its three built-in policy packs
(baseline, reviewer-calibrated, and strict-agent), reading only the monitor view. Their
score is Armour's severity-weighted penalty capped and scaled to the unit interval, so
they tie heavily; their own decision threshold is Armour's fail verdict.

Model monitors form a size ladder of one family with one frozen prompt: Qwen3 at
{{name:0.6B}}, {{name:1.7B}}, {{name:4B}}, and {{name:8B}} parameters. The prompt (hash in
prereg/0003-e2-main-study.md) lists the five violation types and asks for one digit from
zero to nine; the score is the probability-weighted mean digit over the digit tokens among
the model's top next-token probabilities, which gives a continuous score with few ties.
Monitors read the task request, every action with its observation, and the final answer,
and nothing else: labels, provenance, and harness state are never in the monitor view.

Two derived monitors complete the four families. The ensemble rank-normalizes the
reviewer-calibrated rule monitor and the largest model monitor against the benign scores
and averages them. The cascade sends the top share of traffic by the smallest model's
score to the largest model and reviews by the largest model's score among escalated
trajectories.

## 4. Experiments

All four confirmatory experiments were preregistered: the hypotheses or estimands,
metrics, analysis plan, thresholds, sample sizes, seeds, exclusions, and stopping rules
were committed under prereg/ before any data they govern existed. `make prereg-verify`
checks, from git history, that every artifact citing a preregistration (the corpus
manifest, the metadata of every score file, the label audit, and every results file) was
first committed after each preregistration it cites was frozen. Anything else in this
report is labeled exploratory.

E0 (prereg/0001-e0-statistical-engine.md) is a simulation study of every interval method
on a grid of score distributions (two binormal separations, a heavy-tailed benign
distribution, and a tied four-level distribution), sample-size designs with and without
source-run clustering, three prevalences, and three review budgets, with the exact truth
computed for every cell. A cell passes if its simulated coverage is at least the nominal
level minus {{param:1.5}} points minus twice its Monte Carlo standard error.

E1 (prereg/0002-e1-corpus.md) builds the corpus described in Section 3.5.

E2 (prereg/0003-e2-main-study.md) estimates recall, precision, and the false-positive rate
at each prevalence and review budget for every monitor on the primary set: the real benign
runs (one per independent run) and the natural and inserted violations. Its interval
method, composite, was chosen from the banked E0 results before any monitor score existed,
as the method E0 supports best for these designs; it still undercovers in some cells of the
small independent design at the smallest budget, so every headline estimate lists the E0
cells it relies on, and estimates that rely on a failing cell are marked. E2 also reports
fixed-threshold rates for the rule monitors, time to detection from prefix scores, recall
by violation source, type, and subject model, results on the full corpus with variants, and
the size ladder.

E3 (prereg/0004-e3-transfer.md) takes two monitors (the reviewer-calibrated rule monitor and
the {{name:0.6B}} model monitor), tunes each one's threshold in one domain and applies it in
every other, and tunes it at one prevalence and applies it at lower ones, testing whether
the review load stays within a factor of two of the budget and whether recall holds.

E4 (prereg/0005-e4-cascades.md) chooses a cascade on a training half of the source runs
under a compute budget of half the cost of running the expensive monitor on all traffic,
and tests on the other half whether its recall holds and whether it is within a fixed
margin of the expensive monitor alone.

## 5. Results

<!-- @prose results -->

## 6. Limitations and threats to validity

These results describe these monitors on this corpus. The methods transfer to other
monitors and data; the numbers do not.

**Simulated tools and small agents.** Every trajectory comes from eight mock-tool domains
in which a violation is an abstract tool call. The subject agents are two small local
models on a CPU, and their trajectories are short. Frontier agents act over longer
horizons with richer tools, and real violations are rarely a single labeled call, so
recall measured here is not an estimate for any deployed agent.

**How violations arise.** Natural violations come from small models doing ordinary tasks
in mock tools, without any instruction to misbehave or to hide, so by design they are
mistakes rather than concealed behavior (the corpus tables give their types). Inserted
violations are one call chosen by the harness, after which the same agent continues; an
inserted false completion ends the episode at that call, and the same model writes its
summary. They are labeled synthetic, and recall on them can differ from recall on natural
violations, which is why the two are reported separately. Armour's records come
from a few generator scripts and are clustered accordingly. None of the three sources is
an adversary trying to evade a monitor; the study measures monitors against violations
that do not hide.

**Labels.** One oracle labels the real and inserted trajectories from the effects of tool
calls, so its definitions decide what counts (DECISIONS.md D11); Armour's records keep
Armour's benign or violation label, with the type and step mapped by base-rate. A defect
in the oracle, found during E1, counted failed calls on missing entities as violations;
prereg/0006 fixed it before the corpus was built, and the corpus relabeling table states
how many labels changed. The blind audit estimates agreement on the preregistered strata
of its sample, not over the corpus as a whole, and the auditor is a model given the same
definitions, not a human expert, so agreement measures consistency with the definitions
more than correctness of the definitions.

**Prevalence is a design parameter.** The corpus's share of violations is not a
deployment rate. Results at each prevalence assume that the benign corpus stands for
deployment's benign traffic and the violation set for its violations (case-control
reweighting), and that the monitor's score distributions do not change with prevalence.
Precision and reviewer load at a given prevalence inherit both assumptions.

**The benign tail limits every low-budget claim.** At small review budgets the threshold
sits in the extreme upper tail of the benign scores, which only the independent benign
runs inform; procedural variants share their source run's cluster and add no
independent information. The intervals are wide for this reason, and E0 found that the
headline interval method undercovers in a few cells of the small independent design at
the smallest budget. Primary estimates that rely on such cells are marked as not
supported by E0, as the preregistered headline rule requires.

**Deviations from the preregistrations.** Three, all before any monitor score existed. The
E2 plan (prereg/0003) was first committed as a draft with its interval method unset and
completed one commit later; the checks treat a file holding an explicit placeholder as an
unfrozen draft (DECISIONS.md D18 and D19, CORRECTIONS.md). The E1 oracle amendment
(prereg/0006) changed the labeling rule after the first corpus batch began. The E1
insertion amendment (prereg/0007), made before any insertion ran, fixed how insertion
sources are drawn, logged skipped attempts, and had the subject model write the summary of
an inserted false completion. Separately, a review found that the E3 analysis code would
have dropped Armour's benign records, which its preregistration includes; the code was
corrected to follow the preregistration (DECISIONS.md D22).

**Monitors.** The model monitors share one frozen prompt and one scoring rule (the
probability-weighted digit), with no prompt tuning, so the size ladder compares sizes at
one prompt, not each model at its best. No Claude model served as a subject or a monitor in
this environment (Section 7). The ensemble and the cascade are the preregistered pairs only.

**Cost and time.** E4's compute cost is the median wall-clock seconds per scored trace on
this machine (a local model server with four slots and four threads), as prereg/0005
defines it; on other hardware or through an API the costs, and so the best cascade, differ.

**Sequential certification.** Adding runs after looking at a claim and testing again on
all runs tests the claim twice, and its error rate exceeds the nominal level
(DECISIONS.md D17). The certification tools assume the number of runs is fixed in advance,
or that new runs are tested alone.

## 7. Ethics and disclosure

base-rate measures monitors; it does not attack them. Every violation in the corpus is
an abstract action in a simulated tool (for example a mock export of a customer table or
a mock deletion of an audit-log entry). No real credential, system, network, or payload
is involved, and the subject agents act only through in-process simulated tools: their
replies are parsed as structured tool calls and never executed. No part of the project
optimizes agents or trajectories to evade monitors. The local models used are
open-weight models under the Apache {{version:2.0}} license; the Armour records are MIT
licensed. No Claude model was used as a subject or a monitor in this environment, because
the session's billing guard found an API-routing setting (DECISIONS.md D2). The project
itself was built by a Claude Code session operated by Raj Kashikar, which wrote the code,
the mock domains, the oracle and the violation definitions, the corpus pipeline, and these
documents; subagents of that session served as the label auditor, the citation checker,
and the reviewer, so none of them is independent of the authoring model family. There is no
vulnerability to disclose: the study found no way to bypass a deployed monitor, and it
reports weaknesses of the evaluated monitors only as rates.

## 8. Reproducibility

`make reproduce` rebuilds every figure, table, and number in this report from the banked
artifacts, with no model calls. The full pipeline (corpus generation, scoring, and
analysis) is scripted in tools/ and pinned: model files by revision and hash
(configs/models.json), the llama.cpp build by tag and commit, sampling settings and
prompts by hash in each preregistration, and every random seed in the preregistrations.
The preregistrations under prereg/ were committed before the data they govern, and
`make prereg-verify` checks that every artifact citing one, data and results alike, came
after it in git history. `make ship-check` runs the project's
definition-of-done gates on a fresh clone.

## References

<!-- @references -->
