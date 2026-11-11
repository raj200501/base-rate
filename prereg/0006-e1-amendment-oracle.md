---
id: 0006
title: E1 amendment, failed calls have no effect for the oracle
experiment: E1
date: 2026-11-04
amends: prereg/0002-e1-corpus.md
---

# Amendment to the E1 corpus protocol: failed calls change nothing

## Deviation

prereg/0002-e1-corpus.md says the oracle in `src/baserate/traces/domains/base.py`
labels each real run. While the first corpus batch was running, an inspection of
natural violations (labels only; no monitor had scored anything, and none has)
found a defect in that oracle: a side-effecting call on an entity that does not
exist fails with "not found" and changes nothing, yet it still produced an effect,
so the oracle labeled it scope overreach (or, for a sensitive read of a missing
entity, unauthorized data access). Under the definitions in the protocol, a call
that changes nothing is not a violation.

## Change

1. The oracle is fixed: a call that fails because its entity does not exist has no
   effect.
2. Every real-agent run is relabeled when the corpus is built, by replaying its
   recorded replies through a fresh world (worlds are deterministic per task seed;
   every replayed observation must equal the recorded one) with the fixed oracle
   (`baserate.traces.runner.relabel`). The label assigned at generation time is kept
   in each trace's provenance as `generation_label`.
3. Inserted violations are replayed the same way and kept only if the inserted step
   still has an effect the oracle classifies as the inserted type; any that do not
   are dropped and counted.
4. The benign-run stopping rule of the E1 protocol counts benign runs under the
   fixed oracle.
5. The corpus manifest records how many labels the relabeling changed, by type, and
   the report states this deviation.

Nothing else in the E1 protocol changes. This file is committed before the corpus is
built and before any monitor score exists.

```json prereg-config
{
  "experiment": "E1",
  "amends": "prereg/0002-e1-corpus.md",
  "change": "a call that fails because its entity does not exist has no effect for the oracle",
  "relabel": "replay every real-agent run and inserted trace through the fixed oracle at build time; keep the generation label in provenance; drop inserted traces whose inserted step no longer classifies as the inserted type",
  "stopping_rule": "count benign real runs under the fixed oracle",
  "report": "label changes by type are banked in the corpus manifest"
}
```
