# Corrections

Every changed or withdrawn claim gets a dated entry here: what was claimed,
where, what changed, and why.

## 2026-11-12 Claims corrected after the pre-review QA (DECISIONS.md D34, D35)

- E4 (README Key results heading and bullet; REPORT abstract and sections 5.4, 5.5, and 6):
  the cascade's held-out gain over the 8B alone was stated without saying that the held-out
  violations mix natural and synthetic inserted ones, or that E4 reports no recall by
  source. Every E4 statement now says so, and section 5.5 now says the combined monitors'
  gain was mostly on synthetic inserted violations where E2 splits by source (QA claims0).
- The REPORT abstract gave the corpus size with no synthetic share and called every source
  run a run of small local agents; it now gives the synthetic share and its sources, and
  says a few source runs are groups of Armour's records. The README pitch called the same
  count "independent agent runs"; it now says independent source runs (claims1).
- REPORT section 5.2 said the single monitors sit "at or near" the random-review rate, but
  at the largest budget the 1.7B and 4B monitors fall below it, which neither document
  said. Both now say it, and the README heading says single monitors were no better than
  random review (claims2).
- README Highlights and Key results and the REPORT abstract and section 5.2 explained the
  curated-records contrast by the violations alone; they now say that Armour's records are
  scored against Armour's own benign records, so the benign tail differs too (claims3).
- The README gave the label audit's pooled agreement without saying the sample is half
  synthetic by design; it now says so, and that agreement was lower on natural violations
  (docs/DATA.md gives each stratum) (claims4).
- Recall by violation source was rendered at two decimals, which hid that the ensemble's
  natural-violation interval lies just above the random-review rate. Breakdown claims now
  render at three decimals like the other recall claims, and the README says which interval
  excludes the random-review rate (claims5).
- Interval claims withdrawn or narrowed earlier still stood in places: the README diagram's
  "Intervals validated in simulation", the README method summary's interval for the
  false-positive rate, REPORT section 4's "a simulation study of every interval method",
  and unscoped coverage claims in the REPORT abstract, introduction, and related work. Each
  now names the budget-recall and fixed-threshold methods E0 checked, or scopes intervals
  to recall and precision (open issue N1) (claims6).
- The README and REPORT section 3.1 said every interval resamples or counts source runs; the
  label-audit interval counts audited records. Both now scope the statement to monitor
  metrics (claims7).
- The REPORT abstract said transferred thresholds "did not keep" the review load within a
  factor of two, and section 5.3 generalized to teams and product surfaces. The abstract now
  gives the shares of domain pairs that did, section 5.3 speaks of this corpus's mock
  domains, and both documents say that many pairs within the recall-transfer bar had also
  missed the review budget, so H3.2 is weak evidence; the README labels the E3 set as mostly
  procedural variants (claims9).
- The README said "the same monitors look strong on curated records" and the REPORT
  abstract "the larger models"; only the rule monitors and the 4B and 8B models reached
  recall near one there, and both now say so (claims10).
- REPORT section 4 said "All four confirmatory experiments" before describing E0 to E4; it
  now says E0 to E4 (claims11).
- docs/DATA.md said Raj built the corpus; it now says a Claude Code session operated by Raj
  built it, as REPORT section 7 says (claims12).
- README Status said the latest review blocked and that its fixes await the next review, a
  sentence that would turn false once that review lands; it now points to STATUS.md and
  artifacts/review/latest.json for the latest verdict (process0). Its sentence on the
  review-run check now states the rule of DECISIONS.md D35 (a run logged within six hours
  before each review and after the review before it, checked over every committed version
  of the review files and the ledger) and that `make budget` checks only the files at HEAD
  (QA gates5).

## 2026-11-12 Claims corrected after the review of a1b287b

- README Highlights (new in a1b287b) gave the reviewer-calibrated rule's pooled primary-set
  recall, most of whose violations are synthetic insertions, as its recall on violations
  "that arose while small agents did ordinary tasks". It now gives the natural-only row of
  the same analysis, whose interval is wider. The same pooled figure stood behind the older
  README key result on Armour's records and the matching REPORT sentence (section 5.2); both
  now give the natural-only rows for the rule and the 8B model (B7).
- README Highlights said "the interval methods were validated by simulation before use", the
  claim withdrawn on 2026-11-11; they now name the two methods E0 checked and say the other
  intervals were not checked (N4). The matching key-result heading is scoped the same way.
- README Highlights implied that the rule-to-8B cascade's held-out gain (E4), like the main
  study's combined monitors, came mostly from synthetic insertions; E4 has no split by
  source, and the sentence now says so and gives the split only for E2 (N28). The
  dataset-set figure now carries the caveat that the rules were built for those records (N3).
- README Status said a banked review without its own logged run fails `make budget` and
  G14. The review found a case that passed (B6, second round). The check is stricter now
  (DECISIONS.md D32), and the sentence states the limit that remains.

## 2026-11-12 Claims refined after the 2026-11-12 review

- README Status said any overrun other than the waived one fails `make budget`; that held
  only for logged overruns until the B6 fix. It now says that any other overrun, logged or a
  banked review without its own logged run, fails `make budget` and G14, which both now check.
- The README's library bullet credited cluster-aware intervals to the false-positive rate as
  well; it now scopes them to recall and precision and points to open issue N1. Its Status
  section now says the smaller analysis choices of open issue N8 are not in the report's list
  of deviations.
- docs/REPORT.md's abstract and introduction still credited the library with
  threshold-transfer tools; transfer is the E3 analysis, and both now say so. The size-ladder
  paragraph now says the three rule configurations score every primary-set trace
  identically, so its best rule monitor is a tie broken by list order.

## 2026-11-11 README claims made exact before the next review

A development QA pass over the release README (commits 795ea75 to 543f7ef, public since the
release) found claims stated more broadly than the code and artifacts support. Each was
corrected:

- The pitch gave the corpus size without its synthetic share or its independent runs; it
  now gives both and names the synthetic sources.
- The pitch and "What it is" said the library's intervals respect the independent runs for
  the false-positive rate too; at a fixed budget that rate is set by the budget, and its
  printed interval is tighter than the benign runs certify (open issue N1). Both now scope
  the claim to recall and precision.
- "Checks whether thresholds transfer" was listed as a library feature; transfer is tested
  by the E3 analysis, not by a library function, and the README now says so.
- "No number ... is typed by hand" ignored the allowance tags; it now says no result number
  is, and that every other number carries an auditable allowance. Counts written as words in
  the E4 result ("all four pairs") now read "every pair".
- The Status section said E0 to E4 "ran as preregistered", that all five first-review issues
  were recorded in CORRECTIONS.md, and that `make reproduce` rebuilds every result. It now
  cites the reported deviations, says which fixes changed published claims, scopes
  `make reproduce` to E2 to E4 and the documents, and leaves the live gate state to
  STATUS.md instead of a statement that would turn false after the next review.

## 2026-11-11 The 14/14 ship-check at 19dbd5a passed on an incomplete usage ledger

What was claimed: README.md's Status section (commit 795ea75) said that at commit 19dbd5a
`make ship-check` printed PASS 14/14. That is what it printed, but G14's property ("every
cap respected") was false at the time: the session had run `make review` twice that day,
against an effective cap of one review run a day at the project's cap share of 0.2
(DECISIONS.md D9), and neither run had been logged. Logging both (artifacts/usage/ledger.jsonl)
makes `make budget`, and so G14, report the overrun. What changed: the Status section now
says so, DECISIONS.md D25 records the overrun, and STATUS.md asks Raj how to treat it. Caps
are never raised, so the gate stays failing on that day's record until he decides.

Update (2026-11-11): Raj accepted that one overrun. DECISIONS.md D26 records a dated waiver
scoped to that day and kind (budget_waivers.toml); `make budget` and G14 honor exactly that
waiver, every other overrun still fails, and the README Status section says so.

## 2026-11-11 Results claims corrected after the first `make review`

The first rendering of README.md and docs/REPORT.md (commits 238b99d to e403a4e, not yet
public) made claims that the independent review (artifacts/review/, review of e403a4e)
showed wrong or incomplete. Each was corrected before release:

- What was claimed: only the ensemble and the cascade clear chance, with no breakdown by
  violation source for either. What changed: the preregistered breakdown (prereg/0003) was
  missing for both because the analysis skipped derived monitors; it is now computed for
  every primary monitor. Most of their gain is on synthetic inserted violations, and on
  natural violations the cascade's interval contains the random-review rate. The README
  finding now says so on its first screen (review B1).
- What was claimed: time to detection describes the violations a monitor catches within
  the budget. What changed: the analysis counted violations that only a prefix flagged;
  as preregistered it now counts only violations the budget catches by their full trace,
  and the rows changed accordingly (B2).
- What was claimed: the E3 recall-transfer result held only because recall is near zero in
  every domain. What changed: that premise is false for the rule monitor, whose own-domain
  recall varies across domains; the supported outcome is now reported plainly (B3).
- Smaller corrections: the E4 heading said a cheap filter beat the expensive monitor alone,
  which one pair contradicts (N6); the insertion skips logged as no valid call were
  attributed to the subject model, but the runner skips those before the model is asked
  (N5); E0's coverage of interval methods was overstated (N4); the size-ladder crossing
  rule did name a rung (N7).

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
