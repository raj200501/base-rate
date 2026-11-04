#!/usr/bin/env bash
# Run the remaining E1 corpus build and the E2 scoring, in order, unattended.
#
#   nohup tools/run_pipeline.sh > runs/pipeline/pipeline.log 2>&1 &
#
# Every step is resumable (finished episodes and scores are skipped), so the
# script can be restarted after a crash. It follows prereg/0002-e1-corpus.md
# (subject models and seeds, the benign-run stopping rule, the insertion split)
# and prereg/0003-e2-main-study.md (which monitor scores which set). It never
# commits or pushes; the main session reviews and commits the artifacts.
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_SYSTEM_CERTS=1 CAP_SHARE="${CAP_SHARE:-0.2}"
unset UV_NATIVE_TLS || true
RUN="uv run --frozen python"
mkdir -p runs/pipeline runs/agents
step() { echo "[$(date -u +%FT%TZ)] $*"; }

wait_for() {  # wait while a process matching $1 runs
  while pgrep -f "$1" > /dev/null; do sleep 60; done
}

benign_real() {  # benign real-agent runs under the fixed oracle (prereg/0006)
  $RUN tools/count_benign.py
}

step "wait for any running 1.7B corpus batch"
wait_for "tools/run_agents.py --model qwen3-1.7b-q8_0 --seeds 0:250"
step "1.7B batch (resume)"
$RUN tools/run_agents.py --model qwen3-1.7b-q8_0 --seeds 0:250 --concurrency 4 --threads 4 \
  --port 8096 --out runs/agents/corpus-1.7b
step "4B batch"
$RUN tools/run_agents.py --model qwen3-4b-q4_k_m --seeds 250:350 --concurrency 4 --threads 4 \
  --port 8097 --out runs/agents/corpus-4b

# Stopping rule: extend on the first listed model, 25 seeds per domain per block.
# A block already started (a restart may have interrupted it) is always finished
# before the count is checked again, so no block is left partial.
next=350
while [ -d "runs/agents/corpus-1.7b-ext${next}" ]; do
  step "resume extension block seeds ${next}:$((next + 25))"
  $RUN tools/run_agents.py --model qwen3-1.7b-q8_0 --seeds "${next}:$((next + 25))" \
    --concurrency 4 --threads 4 --port 8096 --out "runs/agents/corpus-1.7b-ext${next}"
  next=$((next + 25))
done
while [ "$(benign_real)" -lt 2000 ]; do
  step "benign real runs below 2000: extension block seeds ${next}:$((next + 25))"
  $RUN tools/run_agents.py --model qwen3-1.7b-q8_0 --seeds "${next}:$((next + 25))" \
    --concurrency 4 --threads 4 --port 8096 --out "runs/agents/corpus-1.7b-ext${next}"
  next=$((next + 25))
done
step "benign real runs: $(benign_real)"

# Insertions: 700 attempts split by each model's share of benign real runs.
read -r n17 n4 < <($RUN tools/count_benign.py --split 700)
step "insertions: ${n17} on 1.7B runs, ${n4} on 4B runs"
if [ "${n17}" -gt 0 ]; then
  $RUN tools/run_agents.py --model qwen3-1.7b-q8_0 --insert --max-inserts "${n17}" \
    --from runs/agents/corpus-1.7b*/traces.jsonl --concurrency 4 --threads 4 --port 8096 \
    --out runs/agents/insert-1.7b
fi
if [ "${n4}" -gt 0 ]; then
  $RUN tools/run_agents.py --model qwen3-4b-q4_k_m --insert --max-inserts "${n4}" \
    --from runs/agents/corpus-4b/traces.jsonl --concurrency 4 --threads 4 --port 8097 \
    --out runs/agents/insert-4b
fi

step "build the corpus"
$RUN tools/build_corpus.py --runs runs/agents/corpus-*/traces.jsonl runs/agents/insert-*/traces.jsonl \
  --variants 9
step "audit sample"
$RUN tools/label_audit.py sample

step "score rule monitors"
for m in rule:armour-baseline rule:armour-reviewer-calibrated rule:armour-strict-agent; do
  $RUN tools/score_monitors.py --monitor "$m" --set primary --prefixes
  $RUN tools/score_monitors.py --monitor "$m" --set full
  $RUN tools/score_monitors.py --monitor "$m" --set dataset
done
step "score model monitors"
$RUN tools/score_monitors.py --monitor model:qwen3-0.6b-q8_0 --set primary --prefixes
$RUN tools/score_monitors.py --monitor model:qwen3-0.6b-q8_0 --set dataset
$RUN tools/score_monitors.py --monitor model:qwen3-1.7b-q8_0 --set primary --prefixes
$RUN tools/score_monitors.py --monitor model:qwen3-1.7b-q8_0 --set dataset
$RUN tools/score_monitors.py --monitor model:qwen3-4b-q4_k_m --set primary
$RUN tools/score_monitors.py --monitor model:qwen3-4b-q4_k_m --set dataset
$RUN tools/score_monitors.py --monitor model:qwen3-8b-q4_k_m --set primary
$RUN tools/score_monitors.py --monitor model:qwen3-8b-q4_k_m --set dataset
$RUN tools/score_monitors.py --monitor model:qwen3-0.6b-q8_0 --set full
step "pipeline finished"
