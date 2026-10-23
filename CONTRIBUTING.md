# Contributing

Thank you for considering a contribution.

## Setup

```bash
make setup      # uv sync from uv.lock (Python 3.11 by default)
make test       # tests with coverage
make lint typecheck
```

## Rules that every change follows

- Every public function has type hints, a docstring, and a test.
- `ruff check`, `ruff format --check`, and `mypy --strict` pass with zero errors.
  A `type: ignore` names its error code and carries a reason comment.
- No result number is typed by hand. Every number in README.md, docs/REPORT.md,
  figures, and tables comes from a committed script reading a banked artifact
  under `artifacts/`; `make claimcheck` enforces this.
- Confirmatory analyses follow a preregistration in `prereg/` that was
  committed before the data existed. Changes are new amendment files; frozen
  files are never edited.
- Synthetic and mock data are labeled as such everywhere.
- Scope is defensive: mock tools, abstract violations, no evasion research.
- Keys and tokens stay in environment variables. Run `make secrets-scan`.
- Plain writing: precise, scoped claims, no hype words, no em dashes.

## Before you open a pull request

Run `make ci`. For changes that touch results, also run `make ship-check`.
