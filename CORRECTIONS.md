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

## 2026-11-04 Two inexact statements about the draft-stage rule

1. The first 0003 entry says that, apart from the placeholder and the added
   paragraph, no other line changed. In fact the freezing commit 72fc428 also
   rewrote the four existing lines of the Estimator bullet and removed its
   parenthetical "(chosen from the methods E0 supports for these designs,
   before any monitor score exists)". In the config, only `/headline/method`
   changed (`TO_BE_SET_FROM_E0` to `composite`).
2. The resolution entry says the new rule "keeps every other condition of the
   old check". The first implementation (767570f) did not: it listed only
   ordinary commits, so a merge commit that filled the placeholder was skipped,
   and a later ordinary edit was taken as the freeze. An adversarial review
   found this, together with older gaps that the old check shared (an artifact
   first committed inside a merge before the freeze, and a transient change made
   and undone by merges after it). Both checks now read every version of the
   file in HEAD's history, merges included: the freeze must be an ordinary
   commit, no later commit may introduce another version, every descendant of
   the freeze must hold the frozen content, and an artifact must not exist,
   under any name it was renamed from, at any commit that does not descend from
   the freeze (DECISIONS.md D21). Regression tests cover each history.
