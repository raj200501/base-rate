# Preregistrations

Each confirmatory experiment is preregistered here before any confirmatory
data exists. Files are named `NNNN-short-name.md` and are immutable once
committed: `make ship-check` (gate G11) fails if a file here is ever modified
or deleted. A needed change is a new, dated amendment file that cites the
original and is reported as a deviation.

Each file has YAML-style front matter (`id`, `title`, `experiment`, `date`)
and exactly one fenced block tagged `json prereg-config` holding the frozen
machine-readable plan (grid, thresholds, sample sizes, seeds, exclusions,
stopping rules). Artifacts cite the file in `prereg` and the SHA-256 of the
canonical JSON of that block in `prereg_config_sha256`.

Drafts live outside this directory until they are frozen. The checks also
define a draft stage inside it (DECISIONS.md D19): a file whose config block
holds a string value `TO_BE_SET` or `TO_BE_SET_<WORDS>` declares itself
unfinished, and it is frozen by the first commit whose config holds none.
Every earlier commit must hold a placeholder, the freezing commit may change
the config only where a draft held one, nothing may cite the file or amend it
before it is frozen, and a file that still holds a placeholder fails the
checks. After the freezing commit the file never changes. Only
`0003-e2-main-study.md` went through this stage (draft 8e2836c, frozen
72fc428; CORRECTIONS.md).
