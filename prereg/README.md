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

Drafts live outside this directory until they are frozen.
