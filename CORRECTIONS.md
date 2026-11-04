# Corrections

Every changed or withdrawn claim gets a dated entry here: what was claimed,
where, what changed, and why.

No corrections yet (no claims have been published).

## 2026-11-04 Preregistration 0003 was committed twice

What happened: when the E2, E3, and E4 drafts were moved from docs/drafts/
into prereg/ with `git mv`, the moves were staged, so the next commit (8e2836c,
"Bank E0", 2026-11-04 02:02:33 UTC) included prereg/0003-e2-main-study.md in
its unfinished draft form, with the headline method still the unset
placeholder `TO_BE_SET_FROM_E0`. The following commit (72fc428, the same
minute) replaced the placeholder with `composite` and added the paragraph that
explains the choice from the banked E0 artifact. No other line changed, and no
monitor score existed at either commit (no file under artifacts/scores/ has
ever been committed before these two commits).

Why it matters: the brief's rule is that a committed preregistration is never
edited; a change must be a new amendment file. The edit broke that rule, and
gate G11 (`make prereg-verify`) correctly reports it. The gate is not changed
to accept this. Raj decides the remedy (STATUS.md, "Needs Raj").

What does not change: prereg/0004-e3-transfer.md and prereg/0005-e4-cascades.md
were frozen in 8e2836c and are untouched since. The E2 analysis uses the final
content of 0003, which is also what its config hash records.

## 2026-11-04 Resolution of the preregistration 0003 double commit

The entry above said the gate would not be changed and that Raj would choose
the remedy. Raj delegated the choice, and the checks were changed as recorded
in DECISIONS.md D19 (committed before the change): a preregistration whose
config holds an explicit `TO_BE_SET` placeholder is a self-declared draft, and
it is frozen by its first commit without one. Under that rule 0003 was a draft
at 8e2836c and froze at 72fc428; in the config the freezing commit changed
only the value the draft left unset (the prose gained the paragraph that
explains the choice), and both commits precede every monitor score. The rule
is general, it keeps every other condition of the old check, and mutation
tests show each of its conditions can fail. `make prereg-verify` prints a note
naming both commits, and the G11 line of ship-check names 0003 as frozen after
a declared draft. The history was not rewritten, so the original commit
remains in the log for anyone to inspect.
