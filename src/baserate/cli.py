"""Command-line entry point for ``baserate``.

Subcommands are added as their modules land: ``eval``, ``certify``,
``cascade``, and ``card`` are planned (docs/PLAN.md). ``budget`` exists now.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from baserate import __version__, budget


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(prog="baserate", description=__doc__)
    parser.add_argument("--version", action="version", version=f"baserate {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("budget", help="check usage caps and cash spend", add_help=False)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    known, rest = parser.parse_known_args(args)
    if known.command == "budget":
        return budget.main(rest)
    parser.error(f"unknown command {known.command}")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
