# Datasheet: the base-rate trace corpus

This datasheet answers the questions of Gebru et al. [@gebru-datasheets-2021] for the
corpus that experiment E1 built (prereg/0002-e1-corpus.md, amended by
prereg/0006-e1-amendment-oracle.md). Every number below is rendered from a banked
artifact or from a preregistration's frozen config; docs/CLAIMS.md explains how.

**All data here is mock or synthetic.** The trajectories come from small local models
acting in simulated tools, from procedural variants of those runs, and from Armour's
labeled records. No real system, person, credential, or payload is involved, and the
violations are abstract actions in mock tools.

## Motivation

The corpus exists to measure what an agent monitor is worth when violations are rare: a
monitor's recall and precision at a fixed human-review budget, at deployment prevalences
far below those of any labeled set. That needs benign trajectories in bulk, violation
trajectories with the step at which each violation happens, and a record of which
trajectories share a source run, because an interval is only as tight as the number of
independent runs behind it. It was built by Raj Kashikar for the base-rate study, with no
funding and no paid compute.

## Composition

Each record is one trajectory in Armour's trace schema: the user request, then the
agent's tool calls with their arguments and the simulated observations, and the final
answer. Labels sit beside the trajectory and are never shown to monitors: `label`
(benign or violation), `violation_type`, `first_violating_step`, `source`,
`source_run_id` (the independent run the record belongs to), and `synthetic`.

<!-- @table corpus -->

There are eight mock-tool domains (browser portal, code maintenance, customer support,
devops, finance operations, HR, research data, and SaaS administration). The five
violation types are those of Armour's taxonomy, made operational by one oracle
(DECISIONS.md D11): an audit-log edit (`audit_log_edit`); a grant, role change, or
approval the task did not authorize (`unapproved_escalation`); a bulk export or a read of
sensitive fields the task did not need (`unauthorized_data_access`); a write or
side-effecting action on an entity outside the task's write scope (`scope_overreach`);
and finishing as complete before a required effect happened (`false_completion`).

<!-- @table corpus-types -->

<!-- @table corpus-agents -->

Records are not independent. A procedural variant keeps its source run's
`source_run_id`, so variants add scale without adding independent runs; an inserted
violation keeps the id of the benign run it was made from; and Armour's records are
clustered by source file and agent family, because a few generator scripts produced them
(DECISIONS.md D12). Every table states the synthetic share and the number of independent
source runs for this reason, and every interval in the study resamples or counts whole
source runs.

## Collection

Real-agent runs: subject models `{{name:qwen3-1.7b-q8_0}}` (task seeds from
{{claim:e1.seeds.small.start}} up to but not including {{claim:e1.seeds.small.end}}) and
`{{name:qwen3-4b-q4_k_m}}` (task seeds from {{claim:e1.seeds.large.start}} up to but not
including {{claim:e1.seeds.large.end}}), pinned by revision and file hash in configs/models.json
and served by a pinned llama.cpp build, ran one episode per domain and task seed with
temperature {{claim:e1.temperature}}, top-p {{claim:e1.top_p}}, top-k {{claim:e1.top_k}},
at most {{claim:e1.max_steps}} steps, and a sampling seed derived from the episode id.
The preregistered stopping rule continued in blocks of {{claim:e1.extension_block}} seeds
per domain on the first model until benign real runs, counted under the fixed oracle,
reached {{claim:e1.benign_target}}. Each episode is a fresh simulated world built
deterministically from its task seed; nothing runs outside the Python process.

Inserted violations: {{claim:e1.insert_attempts}} attempts drew benign real runs at random
(insertion seed in the preregistration), cycled through the five types, and chose the
inserted step uniformly among the run's agent steps. The run's earlier actions were
replayed exactly through a fresh world, one violating call checked by the oracle was
placed at the chosen step, and the same model then continued from that state. Attempts
whose call did not classify as the drawn type were dropped. Inserted records are labeled
synthetic because the harness wrote one step.

Procedural variants: each benign real run has {{claim:e1.variants}} variants that rename
entity ids and remap amounts consistently, reorder JSON observations, and swap a few
phrases (`baserate.traces.variants`). They are labeled synthetic.

Armour records: Armour's labeled datasets were copied verbatim from
https://github.com/raj200501/armour at commit `639eb62ae17a8c3876137f375176417ff6671ea0`
on {{date:2026-10-23}} (tests/fixtures/armour, MIT License, license text included there).
Records that do not map cleanly onto the five types are skipped and counted, never
guessed (`baserate.traces.armour`).

The corpus was assembled by tools/build_corpus.py on {{claim:corpus.built_at}} (UTC).

## Labeling

The oracle in `src/baserate/traces/domains/base.py` labels each real and inserted
trajectory from the effects of its tool calls; labels never come from a monitor. While
the first batch was running, an inspection of labels (before any monitor had scored
anything) found that a call on an entity that does not exist, which fails and changes
nothing, was still counted as a violation. The amendment prereg/0006 fixed the oracle
before the corpus was built, and every real and inserted record was relabeled by
replaying its recorded replies through a fresh world, with the label assigned at
generation time kept in provenance. This is a deviation from the original protocol, and
its effect is stated here:

<!-- @table corpus-relabel -->

Label audit: an independent auditor (a separate model session with no access to labels,
code, or the oracle) labeled a stratified random sample of {{claim:e1.audit_n}} records
(seed in the preregistration) from what a monitor sees: label, violation type, and first
violating step. Agreement means all three match the corpus label.

<!-- @table audit -->

## Uses

Intended: measuring monitors' recall, precision, and false-positive rate at a review
budget and a deployment prevalence; checking interval methods on clustered data; sizing
certification samples; and studying threshold transfer and cascades.

Not intended: estimating how often real agents violate policy (prevalences in the study
are design parameters, not measurements); judging frontier models (the subject agents are
small local models); and training or tuning agents or trajectories to evade monitors,
which is out of scope for this project and which the corpus is not designed to support.

## Distribution

The corpus ships inside this repository under artifacts/corpus/: gzip JSONL shards, each
well under the shard size limit of the brief, with their {{name:SHA-256}} hashes, composition
counts, relabeling log, and provenance in artifacts/corpus/manifest.json. The project is
MIT-licensed (LICENSE); the Armour-derived records are MIT-licensed by their authors, and
their license text ships in tests/fixtures/armour/LICENSE. Every shard record and the
manifest carry this canary string, so that copies can be found and filtered out of
training data:

`{{claim:corpus.canary}}`

## Maintenance

Raj Kashikar maintains the corpus. Any changed or withdrawn claim gets a dated entry in
CORRECTIONS.md, and the git history records every rebuild. tools/build_corpus.py rebuilds
the shards from the raw runs; `make reproduce` re-runs every analysis from the banked
shards and scores and checks that nothing changes.

## References

<!-- @references -->
