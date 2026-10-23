# CLAUDE.md

After any compaction or restart, re-read CLAUDE.md, STATUS.md, and sections 6 to 10 of docs/BUILD_BRIEF.md before acting.

## Mission

Build **base-rate**: open, statistically rigorous tooling and evidence for what an AI agent monitor is worth at realistic prevalence. The `baserate` library estimates rare-event recall and precision at a fixed human-review budget with valid intervals, sizes certification samples, checks threshold transfer, and optimizes budget-constrained cascades. A labeled trace corpus in Armour's schema (benign trajectories across mock-tool domains plus step-labeled abstract violations) feeds a preregistered study across rule monitors, a local open-weight size ladder, Claude tiers, ensembles, and cascades. The brief is docs/BUILD_BRIEF.md (immutable; its SHA-256 is in docs/BUILD_BRIEF.sha256). Done means `make ship-check` prints `SHIP-CHECK: PASS 14/14`.

## Non-negotiables (docs/BUILD_BRIEF.md section 9, verbatim)

These hold on every turn. Copy them verbatim into CLAUDE.md during bootstrap so they survive context compaction.

1. **No fabricated or hand-typed numbers.** Every result number (a measured rate, count of outcomes, estimate, interval, or test statistic) in README.md, docs/REPORT.md, other results documents, figures, and tables is produced by a committed script from a banked artifact under `artifacts/`. Design parameters, thresholds, caps, dates, versions, and the text of docs/BUILD_BRIEF.md are not result numbers. `make claimcheck` enforces this and `make freshness` re-derives every live number.
2. **Preregister before confirmatory runs.** Hypotheses, metrics, analysis plan, thresholds, sample sizes, exclusions, and stopping rules go in `prereg/NNNN-*.md`, committed and hashed before any confirmatory data exists. A needed change is a new, dated amendment file that cites the original, is committed before any data it affects, and is reported as a deviation; the original is never edited. Anything not preregistered is labeled exploratory.
3. **Publish failures beside successes.** Null and negative results get the same prominence as positive ones. Every changed or withdrawn claim gets a dated entry in CORRECTIONS.md.
4. **Label synthetic and mock data everywhere.** Synthetic, procedurally generated, or mock results are never presented as model results or real-world results.
5. **$0 cash, with caps enforced in code** (section 8).
6. **Secrets stay in the environment.** Never write keys or tokens to files, logs, artifacts, or command lines. `make secrets-scan` runs in CI and in ship-check.
7. **Defensive scope only.** Mock tools and disposable sandboxes. No real credentials, real systems, or harmful payloads. Build nothing that helps an agent evade graders, monitors, or safety measures beyond what the measurement strictly requires.
8. **No outbound actions.** Never send email, open issues or pull requests, post, publish packages, datasets, or sites, or contact anyone. Pushing is allowed only under section 10.1 step 11. Prepare drafts in `outbound/` for Raj to review and send.
9. **Respect licenses and terms.** Record the source, license, and retrieval date of all third-party data in docs/DATA.md. Do not redistribute what a license does not allow. Add a canary string to any dataset meant for release.
10. **The brief is immutable.** Never edit docs/BUILD_BRIEF.md. Record deviations, substitutions, and interpretations in DECISIONS.md, with a reason, before acting on them.
11. **Verified citations only.** Never cite from memory. Open and check every reference (title, authors, year, URL) once and bank what you saw in `artifacts/citecheck.json` (retrieval date, final URL, page title, content hash); `make citecheck` validates that file offline, so ship-check never needs the network. If a site blocks scripted access, use the arXiv, DOI, or another official copy, and never work around bot protection. If nothing opens, drop the reference and list it under "Needs Raj" for a manual check.
12. **Plain writing.** Precise, scoped claims. No hype words such as "revolutionary", "groundbreaking", or "state of the art". No em dashes anywhere (Raj's style rule).
13. **Honest progress.** Never pass a gate by weakening its check. If a gate cannot pass, say so in STATUS.md and keep working on what can.
14. **Abstract violations only.** Violations are abstract actions in mock tools (for example a mock `customer.export_all` call). No real credentials, systems, or payloads, and no realistic attack content.
15. **No evasion research.** Optimizing agents or trajectories to slip past monitors is out of scope. This project measures monitors; it does not attack them.

## State files

- `STATUS.md`: line 1 `goal_turn: N`, line 2 `STATE: ACTIVE | BLOCKED ON RAJ | LADDER COMPLETE`; gate table, Now, next three actions, usage, Needs Raj, Blocked, backlog. Under 200 lines; history moves to docs/LOG.md.
- `DECISIONS.md`: assumptions, deviations, substitutions, interpretations, each with a reason, written before acting on them.
- `CORRECTIONS.md`: dated entry for every changed or withdrawn claim.
- `docs/PLAN.md`: milestones mapped to gates, risks, fallbacks, usage estimates, artifact contracts.
- `docs/LOG.md`: iteration history. `docs/REVIEW_LOG.md`: non-blocking review issues.
- `budget.toml`: caps (lower only, never raise). `artifacts/usage/ledger.jsonl`: every model call.
- `prereg/`: frozen preregistrations (immutable once committed). `artifacts/`: banked results with provenance.
- `.claude/loop.md`: the heartbeat prompt. `.claude/agents/`: `reviewer`, `citation-checker`.
- `private/`, `runs/`: git-ignored. `outbound/`: drafts for Raj to send; never send anything.

## Commands

- `python tools/ship_check.py --fast`: orient (does not count). `make ship-check`: the full check that counts.
- `make setup`, `make doctor`, `make test`, `make lint`, `make typecheck`, `make ci`, `make demo`.
- `make prereg-verify`, `make claimcheck`, `make freshness`, `make citecheck`, `make secrets-scan`, `make budget`.
- `make review` (headless opus when the billing guard allows, else run the `reviewer` subagent; never edit artifacts/review/latest.json yourself), `make report`, `make reproduce`.

## Working rules

- One Loop Protocol iteration per heartbeat (brief 10.2): the next failing gate on the critical path, then Stretch Ladder rungs. Test-first, small commits.
- A subagent never declares a gate passed; only `make ship-check` does.
- Never launch Claude children while `make doctor` reports a billing-guard problem. At most one model-heavy run at a time; long runs go to the background with a PID file and log under `runs/<id>/`.
- Push only under brief 10.1 step 11 and the push decision in DECISIONS.md. Never force-push, push tags, or change repository settings.
- Commit messages and repository files never name the model serving this session; experiment provenance records the models under test.
