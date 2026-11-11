"""Ship check: gates G1 to G14 of docs/BUILD_BRIEF.md section 6 (``make ship-check``).

First checks, in the original checkout, the parts of G14 about the working
tree, private/, and private hash manifests. Then clones HEAD into a temporary
directory and runs every other check there. It verifies banked artifacts
(presence, provenance, hashes, preregistration links, thresholds) and
recomputes statistics from them; it never runs an experiment or calls a model.
It writes nothing inside the working tree; sub-command logs go to the
temporary directory (``--keep`` keeps it and names it on stderr).

stdout is exactly ``HEAD <sha> fresh-clone``, then one ``G<n> <PASS|FAIL>
<evidence>`` line per gate, then ``SHIP-CHECK: PASS 14/14`` or
``SHIP-CHECK: FAIL <passed>/14``. With ``--fast`` the slow steps listed in
FAST_SKIPS are skipped and the last line is ``SHIP-CHECK-FAST (does not
count): <passed>/14``. Only a full run counts.

The artifact contracts checked here are documented in docs/PLAN.md.
Stdlib only, so it runs before ``make setup``.
"""

from __future__ import annotations

import argparse
import ast
import base64
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

GATES = 14
EVIDENCE_MAX = 220
FAST_SKIPS = {
    3: "make ci not run (composed from G1, G2, G4)",
    9: "pip install -e into a fresh venv not run (examples use the uv env)",
    10: "make freshness not run",
}
COVERAGE_FLOOR = 85.0
CORE_PACKAGES = ("traces", "monitors", "estimate", "certify", "cascade")
DEMO_LIMIT_S = 180.0
DEMO_GOLDEN = Path("demo/golden_output.txt")
EXAMPLES = (
    Path("examples/evaluate_your_monitor.py"),
    Path("examples/certify.py"),
    Path("examples/build_cascade.py"),
)
SHARD_LIMIT_BYTES = 50 * 1024 * 1024
E1_TARGETS = {"benign": 20_000, "benign_real": 2_000, "violations": 500, "violation_types": 5}
E2_PREVALENCES = (1e-2, 1e-3, 1e-4)
E2_BUDGETS = (5, 20, 100)
E2_MIN_FAMILIES = 4
E2_METRICS = ("recall_at_budget", "precision_at_budget")
E2_PREREG_METRICS = ("recall_at_budget", "precision_at_budget", "fpr_at_budget")
E2_BREAKDOWN_GROUPS = ("violation_source", "violation_type", "subject_model")
E0_MIN_REPLICATES = 2000
E0_MIN_REPLICATES_BOOTSTRAP = 500
E0_TOLERANCE = 0.015
REPORT_WORDS = (4000, 8000)
DEFAULT_CAPS = {
    "claude_agent_runs_per_day": 30,
    "claude_agent_max_turns": 40,
    "claude_single_calls_per_day": 300,
    "claude_review_runs_per_day": 6,
    "local_model_max_params_b": 8,
    "disk_floor_gb": 8,
}
CAP_FLOORS = {"disk_floor_gb"}
REVIEW_DRIFT_FILES = ("STATUS.md", "docs/LOG.md")
REVIEW_DRIFT_DIRS = ("artifacts/review/",)
README_SECTIONS = (
    ("what it is", "what it is and is not"),
    ("key results", "key results"),
    ("how it works", "how it works"),
    ("use", "how to use it on your own benchmark or agent"),
    ("reproduc", "how to reproduce every figure"),
    ("limitation", "limitations and threats to validity"),
    ("disclosure", "responsible-disclosure note"),
    ("related work", "verified related work"),
    ("citation", "citation"),
    ("license", "license"),
)
REPORT_SECTIONS = (
    "abstract",
    "introduction",
    "related work",
    "method",
    "experiments",
    "results",
    "limitations",
    "ethics",
    "reproducibility",
)
DATASHEET_SECTIONS = (
    "motivation",
    "composition",
    "collection",
    "labeling",
    "uses",
    "distribution",
    "maintenance",
)
HYPE_WORDS = (
    "revolutionary",
    "groundbreaking",
    "state of the art",
    "state-of-the-art",
    "cutting-edge",
    "game-changing",
    "unprecedented",
)
BRIEF_MARKER = "This README is a build brief"
EM_DASH = chr(0x2014)


@dataclass
class Gate:
    """Outcome of one gate."""

    n: int
    passed: bool
    evidence: str


@dataclass
class Proc:
    """Result of one sub-command."""

    code: int
    out: str
    secs: float

    @property
    def ok(self) -> bool:
        return self.code == 0

    def tail(self, n: int = 1) -> str:
        lines = [ln for ln in self.out.splitlines() if ln.strip()]
        return " | ".join(lines[-n:]) if lines else ""

    def status(self, prefix: str) -> str:
        """The last line starting with ``prefix`` (a tool's status line), or "".

        Output is stdout then stderr, so a warning on stderr (for example uv's
        deprecation notices) can follow the status line; the tail is not enough.
        """
        lines = [ln.strip() for ln in self.out.splitlines() if ln.strip().startswith(prefix)]
        return lines[-1] if lines else ""


@dataclass
class Ctx:
    """Shared state for one ship-check run."""

    orig: Path
    clone: Path
    tmp: Path
    fast: bool
    env: dict[str, str]
    results: dict[str, Any] = field(default_factory=dict)
    log_index: int = 0

    def run(
        self,
        cmd: list[str],
        name: str,
        timeout: float = 1800,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> Proc:
        """Run ``cmd`` (default cwd: the clone), logging to the temp dir."""
        self.log_index += 1
        log = self.tmp / "logs" / f"{self.log_index:02d}-{name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        env = {**self.env, **(extra_env or {})}
        start = time.monotonic()
        try:
            p = subprocess.run(
                cmd,
                cwd=cwd or self.clone,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            out, code = p.stdout + p.stderr, p.returncode
        except subprocess.TimeoutExpired as exc:
            out = f"TIMEOUT after {timeout}s\n{_text(exc.stdout)}{_text(exc.stderr)}"
            code = 124
        except OSError as exc:
            out, code = f"cannot run {cmd[0]}: {exc}", 127
        secs = time.monotonic() - start
        log.write_text(f"$ {' '.join(cmd)}\n(exit {code}, {secs:.1f}s)\n{out}")
        return Proc(code, out, secs)

    def make(self, target: str, timeout: float = 1800, **variables: str) -> Proc:
        """Run ``make <target>`` in the clone, caching by target and variables."""
        key = f"make:{target}:{sorted(variables.items())}"
        if key not in self.results:
            args = ["make", "--no-print-directory", target]
            args += [f"{k}={v}" for k, v in variables.items()]
            self.results[key] = self.run(args, f"make-{target}", timeout=timeout)
        result: Proc = self.results[key]
        return result


def _text(data: str | bytes | None) -> str:
    """Decode subprocess output that may be bytes, str, or None."""
    if data is None:
        return ""
    return data.decode(errors="replace") if isinstance(data, bytes) else data


class GateError(Exception):
    """Raised inside a gate check to fail it with a short reason."""


def need(cond: bool, reason: str) -> None:
    """Fail the current gate with ``reason`` unless ``cond`` holds."""
    if not cond:
        raise GateError(reason)


# ---------------------------------------------------------------- helpers


def git(repo: Path, *args: str) -> str:
    """Run git and return stripped stdout; raises on failure."""
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file's bytes."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_sha256(obj: Any) -> str:
    """SHA-256 of canonical JSON (sorted keys, no whitespace)."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def load_json(path: Path) -> Any:
    """Parse a JSON file, failing the gate with a readable reason."""
    need(path.is_file(), f"{path.name} missing")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise GateError(f"{path.name} is not valid JSON: {exc}") from exc


def rel(ctx: Ctx, path: str | Path) -> Path:
    """Resolve a repository-relative path inside the clone, refusing escapes."""
    p = (ctx.clone / path).resolve()
    need(p.is_relative_to(ctx.clone.resolve()), f"path escapes repository: {path}")
    return p


def tracked(ctx: Ctx, pathspec: str) -> list[str]:
    """Tracked files in the clone matching a pathspec (NUL-separated, so no path quoting)."""
    out = git(ctx.clone, "ls-files", "-z", "--", pathspec)
    return [p for p in out.split("\0") if p]


def first_commit(repo: Path, path: str) -> str | None:
    """SHA of the first commit that added ``path``, or None if never committed."""
    out = git(repo, "log", "--format=%H", "--no-renames", "--diff-filter=A", "--", path)
    shas = out.splitlines()
    return shas[-1] if shas else None


# A prereg-config string value that marks the file as an unfinished draft (DECISIONS.md D19).
# Matched with fullmatch, so a trailing newline or other text is not a placeholder.
PLACEHOLDER_RE = re.compile(r"TO_BE_SET(?:_[A-Z0-9]+)*")
REGULAR_FILE_MODES = ("100644", "100755")


def has_placeholder(value: Any) -> bool:
    """Whether a JSON value holds a draft placeholder at any depth."""
    if isinstance(value, str):
        return PLACEHOLDER_RE.fullmatch(value) is not None
    if isinstance(value, dict):
        return any(has_placeholder(v) for v in value.values())
    if isinstance(value, list):
        return any(has_placeholder(v) for v in value)
    return False


def keeps_fixed_values(draft: Any, final: Any) -> bool:
    """Whether ``final`` equals ``draft`` everywhere ``draft`` holds no placeholder."""
    if isinstance(draft, str) and PLACEHOLDER_RE.fullmatch(draft):
        return True
    if isinstance(draft, dict):
        return (
            isinstance(final, dict)
            and set(draft) == set(final)
            and all(keeps_fixed_values(draft[k], final[k]) for k in draft)
        )
    if isinstance(draft, list):
        return (
            isinstance(final, list)
            and len(draft) == len(final)
            and all(keeps_fixed_values(d, f) for d, f in zip(draft, final, strict=True))
        )
    return type(draft) is type(final) and bool(draft == final)


def strict_json_object(text: str) -> dict[str, Any] | None:
    """A JSON object with no duplicate keys and no NaN or Infinity, or None."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [k for k, _ in items]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate key")
        return dict(items)

    def constant(name: str) -> Any:
        raise ValueError(name)

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


_HISTORY: dict[str, tuple[list[str], dict[str, tuple[str, ...]]]] = {}


def repo_history(repo: Path) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    """HEAD's commits, parents before children, and each one's parents (cached per HEAD)."""
    key = f"{repo}@{git(repo, 'rev-parse', 'HEAD')}"
    if key not in _HISTORY:
        order: list[str] = []
        parents: dict[str, tuple[str, ...]] = {}
        out = git(repo, "rev-list", "--topo-order", "--reverse", "--parents", "HEAD")
        for line in out.splitlines():
            commit, *ps = line.split()
            order.append(commit)
            parents[commit] = tuple(ps)
        _HISTORY[key] = (order, parents)
    return _HISTORY[key]


def file_states(repo: Path, path: str) -> dict[str, tuple[str, str] | None]:
    """(blob, mode) of ``path`` at every commit of HEAD's history, merges included."""
    order, parents = repo_history(repo)
    out = git(
        repo,
        "log",
        "--full-history",
        "--diff-merges=separate",
        "--no-renames",
        "--raw",
        "--no-abbrev",
        "--format=commit %H",
        "--",
        path,
    )
    changed: dict[str, tuple[str, str] | None] = {}
    commit = ""
    for line in out.splitlines():
        if line.startswith("commit "):
            commit = line.split()[1]
        elif line.startswith(":") and commit:
            fields = line.split("\t")[0].split()
            changed[commit] = None if set(fields[3]) == {"0"} else (fields[3], fields[1])
    states: dict[str, tuple[str, str] | None] = {}
    for c in order:
        states[c] = changed[c] if c in changed else (states[parents[c][0]] if parents[c] else None)
    return states


def descendant_set(repo: Path, commit: str) -> set[str]:
    """Commits of HEAD's history that have ``commit`` as a strict ancestor."""
    return set(git(repo, "rev-list", "--ancestry-path", f"{commit}..HEAD").split())


def renamed_from(repo: Path, path: str) -> list[str]:
    """``path`` and every name it was renamed from (renames only; copies are not followed)."""
    out = git(
        repo,
        "log",
        "--full-history",
        "--diff-merges=separate",
        "-M",
        "--diff-filter=R",
        "--name-status",
        "-z",
        "--format=",
        "HEAD",
    )
    sources: dict[str, set[str]] = {}
    fields = [f for f in out.split("\0") if f]
    i = 0
    while i < len(fields):
        status = fields[i].strip()
        width = 3 if status[:1] in {"R", "C"} else 2
        if status.startswith("R") and i + 2 < len(fields):
            sources.setdefault(fields[i + 2], set()).add(fields[i + 1])
        i += width
    names = [path]
    for name in names:
        names.extend(sorted(sources.get(name, set()) - set(names)))
    return names


def version_config(repo: Path, state: tuple[str, str] | None) -> dict[str, Any] | None:
    """The config of one committed version of a preregistration, or None if invalid."""
    if state is None or state[1] not in REGULAR_FILE_MODES:
        return None
    text = git(repo, "cat-file", "blob", state[0])
    blocks = re.findall(r"```json prereg-config\n(.*?)```", text, re.S)
    return strict_json_object(blocks[0]) if len(blocks) == 1 else None


@dataclass
class Freeze:
    """How a preregistration was frozen, from every version in HEAD's history."""

    commit: str | None
    first: str | None
    drafts: list[str]
    introduced: list[str]
    states: dict[str, tuple[str, str] | None]


def freeze_of(repo: Path, path: str) -> Freeze:
    """Find the freezing commit: the first to introduce a version without a placeholder (D19).

    A commit introduces a version when the file exists there and differs from
    every parent, so merge commits count like ordinary ones.
    """
    order, parents = repo_history(repo)
    states = file_states(repo, path)
    intro = [
        c
        for c in order
        if states[c] is not None and all(states[q] != states[c] for q in parents[c])
    ]
    for i, c in enumerate(intro):
        if not has_placeholder(version_config(repo, states[c])):
            return Freeze(c, intro[0], intro[:i], intro, states)
    return Freeze(None, intro[0] if intro else None, intro, intro, states)


def freeze_commit(repo: Path, path: str) -> tuple[str | None, list[str]]:
    """(commit that froze a preregistration, the draft commits before it)."""
    f = freeze_of(repo, path)
    return f.commit, f.drafts


def drift_allowed(path: str) -> bool:
    """Whether a path may change between the reviewed commit and HEAD (brief 10.1 step 8)."""
    return path in REVIEW_DRIFT_FILES or any(path.startswith(d) for d in REVIEW_DRIFT_DIRS)


def id_logged(issue_id: str, text: str) -> bool:
    """Whether ``issue_id`` appears in ``text`` as a whole token (N1 does not match N12)."""
    return re.search(rf"(?<![\w-]){re.escape(issue_id)}(?![\w-])", text) is not None


def is_strict_ancestor(repo: Path, older: str, newer: str) -> bool:
    """True if ``older`` is an ancestor of ``newer`` and not the same commit."""
    if older == newer:
        return False
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", older, newer],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def network_isolation() -> list[str]:
    """Command prefix that runs a child without network access, if available."""
    if sys.platform.startswith("linux") and shutil.which("unshare"):
        probe = subprocess.run(["unshare", "-rn", "true"], capture_output=True, check=False)
        if probe.returncode == 0:
            return ["unshare", "-rn"]
    return []


def offline_env() -> dict[str, str]:
    """Environment overrides that make network use fail fast."""
    dead = "http://127.0.0.1:9"
    return {
        "UV_OFFLINE": "1",
        "HTTP_PROXY": dead,
        "HTTPS_PROXY": dead,
        "http_proxy": dead,
        "https_proxy": dead,
        "ALL_PROXY": dead,
        "NO_PROXY": "",
        "no_proxy": "",
        "BASERATE_OFFLINE": "1",
    }


def md_sections(text: str, level: int = 2) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) pairs at the given heading level."""
    pattern = re.compile(rf"^{'#' * level}[ \t]+(.+?)[ \t]*$", re.M)
    marks = list(pattern.finditer(_strip_code(text)))
    return [
        (m.group(1).strip(), text[m.end() : marks[i + 1].start() if i + 1 < len(marks) else None])
        for i, m in enumerate(marks)
    ]


def _strip_code(text: str) -> str:
    """Blank out fenced code blocks, preserving offsets."""
    return re.sub(r"```.*?```", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)


def word_count(text: str) -> int:
    """Words outside fenced code blocks."""
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-.]*", _strip_code(text)))


def prereg_config(ctx: Ctx, prereg_path: str) -> tuple[Path, Any]:
    """Parse the ```json prereg-config``` block of a preregistration."""
    need(prereg_path.startswith("prereg/"), f"prereg path not under prereg/: {prereg_path}")
    p = rel(ctx, prereg_path)
    need(p.is_file(), f"cited preregistration missing: {prereg_path}")
    blocks = re.findall(r"```json prereg-config\n(.*?)```", p.read_text(encoding="utf-8"), re.S)
    need(len(blocks) == 1, f"{prereg_path} needs exactly one ```json prereg-config``` block")
    try:
        return p, json.loads(blocks[0])
    except ValueError as exc:
        raise GateError(f"{prereg_path} prereg-config is not JSON: {exc}") from exc


def check_prereg_link(ctx: Ctx, artifact_rel: str, artifact: dict[str, Any]) -> Any:
    """Verify an artifact's preregistration citation; return the frozen config.

    The cited file must exist, its config hash must match the artifact's
    ``prereg_config_sha256``, and every commit of HEAD's history at which the
    artifact exists, under its name or any name it was renamed from, merges
    included, must strictly descend from the commit that froze the
    preregistration (not from a draft of it).
    """
    present = {
        c
        for name in renamed_from(ctx.clone, artifact_rel)
        for c, state in file_states(ctx.clone, name).items()
        if state is not None
    }
    need(bool(present), f"{artifact_rel} is not committed")
    amendments = artifact.get("prereg_amendments", [])
    need(isinstance(amendments, list), f"{artifact_rel}: prereg_amendments is not a list")
    configs = []
    for citation in [artifact, *amendments]:
        need(isinstance(citation, dict), f"{artifact_rel}: a prereg citation is not an object")
        prereg_path = str(citation.get("prereg", ""))
        _, config = prereg_config(ctx, prereg_path)
        need(
            citation.get("prereg_config_sha256") == canonical_sha256(config),
            f"{artifact_rel}: prereg_config_sha256 does not match {prereg_path}",
        )
        frozen = freeze_of(ctx.clone, prereg_path).commit
        need(frozen is not None, f"{prereg_path} is not frozen, so {artifact_rel} cannot cite it")
        assert frozen is not None
        need(
            present <= descendant_set(ctx.clone, frozen),
            f"{prereg_path} was not committed before {artifact_rel}",
        )
        configs.append(config)
    return configs[0]


def junit_cases(path: Path) -> list[tuple[str, str]]:
    """(classname, outcome) for each pytest test case in a JUnit XML file."""
    if not path.is_file():
        return []
    cases = []
    for tc in ET.parse(path).getroot().iter("testcase"):
        outcome = "passed"
        for child in tc:
            if child.tag in {"failure", "error"}:
                outcome = "failed"
            elif child.tag == "skipped":
                outcome = "skipped"
        cases.append((tc.get("classname", ""), outcome))
    return cases


def trunc(text: str) -> str:
    """One line, at most EVIDENCE_MAX characters."""
    line = " ".join(text.split())
    return line if len(line) <= EVIDENCE_MAX else line[: EVIDENCE_MAX - 3] + "..."


# ---------------------------------------------------------- original checkout


def original_checks(orig: Path) -> dict[str, str | None]:
    """G14 parts that must run in the original checkout. None means OK."""
    status = git(orig, "status", "--porcelain", "--untracked-files=all")
    dirty = status.splitlines()
    private_tracked = [p for p in git(orig, "ls-files", "--", "private/").splitlines() if p]
    manifest_problem = None
    for manifest in [p for p in git(orig, "ls-files", "--", "*private*.sha256").splitlines() if p]:
        for line in (orig / manifest).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            digest, _, name = line.partition("  ")
            target = orig / name.strip()
            if not name.strip().startswith("private/") or not target.is_file():
                manifest_problem = f"{manifest}: {name.strip()} missing"
            elif sha256_file(target) != digest.strip():
                manifest_problem = f"{manifest}: hash mismatch for {name.strip()}"
    return {
        "tree": f"working tree dirty ({len(dirty)} paths)" if dirty else None,
        "private": f"{len(private_tracked)} files under private/ tracked"
        if private_tracked
        else None,
        "manifest": manifest_problem,
    }


# ------------------------------------------------------------------ gates


def g1(ctx: Ctx) -> str:
    """Build and tests: setup, tests, coverage floor on core modules."""
    setup = ctx.make("setup", timeout=1800)
    ctx.results["setup_ok"] = setup.ok
    need(setup.ok, f"make setup failed (exit {setup.code}): {setup.tail()}")
    cov_json, junit = ctx.tmp / "coverage.json", ctx.tmp / "junit.xml"
    test = ctx.make("test", COV_JSON=str(cov_json), JUNIT_XML=str(junit))
    ctx.results["test_ok"] = test.ok
    ctx.results["junit"] = junit
    cases = junit_cases(junit)
    passed = sum(1 for _, o in cases if o == "passed")
    need(test.ok, f"make test failed (exit {test.code}): {test.tail()}")
    missing = [
        pkg
        for pkg in CORE_PACKAGES
        if not rel(ctx, f"src/baserate/{pkg}").is_dir()
        and not rel(ctx, f"src/baserate/{pkg}.py").is_file()
    ]
    need(not missing, f"{passed} tests passed; core modules missing: {', '.join(missing)}")
    pyproject = tomllib.loads(rel(ctx, "pyproject.toml").read_text(encoding="utf-8"))
    cov_cfg = pyproject.get("tool", {}).get("baserate", {}).get("coverage", {})
    floor = float(cov_cfg.get("floor_percent", COVERAGE_FLOOR))
    need(floor >= COVERAGE_FLOOR, f"coverage floor lowered to {floor}")
    excluded: dict[str, str] = {}
    for item in cov_cfg.get("excluded_adapters", []):
        module, contract = str(item.get("module", "")), str(item.get("contract_test", ""))
        need(bool(module and contract), "excluded adapter entry needs module and contract_test")
        need(rel(ctx, contract).is_file(), f"contract test {contract} missing for {module}")
        stem = "tests." + Path(contract).with_suffix("").name
        results = [o for c, o in cases if c == stem or c.startswith(stem + ".")]
        need(
            bool(results) and all(o == "passed" for o in results),
            f"contract test {contract} did not pass",
        )
        excluded[module] = contract
    data = load_json(cov_json)
    stmts = covered = 0
    for path, info in data.get("files", {}).items():
        norm = path.replace("\\", "/")
        if "baserate/" not in norm:
            continue
        module = norm[norm.index("baserate/") :]
        if module in excluded:
            continue
        stmts += int(info["summary"]["num_statements"])
        covered += int(info["summary"]["covered_lines"])
    need(stmts > 0, "coverage report has no core statements")
    pct = 100.0 * covered / stmts
    need(pct >= floor, f"core line coverage {pct:.1f}% < {floor:g}%")
    return (
        f"setup ok; {passed} tests passed; core coverage {pct:.1f}% (floor {floor:g}%, "
        f"{len(excluded)} adapters excluded with contract tests)"
    )


def type_ignores_without_reason(ctx: Ctx) -> list[str]:
    """Locations of ``type: ignore`` comments lacking an error code and a reason."""
    good = re.compile(r"#\s*type:\s*ignore\[[\w\-, ]+\]\s*#\s*\S.{4,}")
    bad = []
    for f in tracked(ctx, "*.py"):
        src = rel(ctx, f).read_text(encoding="utf-8")
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
        except (tokenize.TokenError, SyntaxError):
            bad.append(f"{f}: cannot tokenize")
            continue
        for tok in tokens:
            if tok.type == tokenize.COMMENT and re.search(r"type:\s*ignore", tok.string):
                line = src.splitlines()[tok.start[0] - 1]
                if not good.search(line[tok.start[1] :]):
                    bad.append(f"{f}:{tok.start[0]}")
    return bad


def g2(ctx: Ctx) -> str:
    """Static quality: ruff, ruff format, mypy --strict, reasoned type ignores."""
    need(ctx.results.get("setup_ok", False), "make setup failed (see G1)")
    uv = ["uv", "run", "--frozen"]
    lint = ctx.run([*uv, "ruff", "check", "."], "ruff-check", timeout=600)
    fmt = ctx.run([*uv, "ruff", "format", "--check", "."], "ruff-format", timeout=600)
    mypy = ctx.run([*uv, "mypy", "--strict", "src/baserate"], "mypy", timeout=1200)
    ctx.results["static_ok"] = lint.ok and fmt.ok and mypy.ok
    need(lint.ok, f"ruff check: {lint.tail()}")
    need(fmt.ok, f"ruff format --check: {fmt.tail()}")
    need(mypy.ok, f"mypy --strict: {mypy.tail()}")
    bad = type_ignores_without_reason(ctx)
    ctx.results["static_ok"] = not bad
    need(not bad, f"type: ignore without code and reason at {', '.join(bad[:5])}")
    return f"ruff check, ruff format --check, mypy --strict clean ({mypy.tail()})"


def g3(ctx: Ctx) -> str:
    """CI parity: workflow runs make ci on 3.11 and 3.12; make ci passes locally."""
    wf = rel(ctx, ".github/workflows/ci.yml")
    need(wf.is_file(), ".github/workflows/ci.yml missing")
    text = wf.read_text(encoding="utf-8")
    need(
        re.search(r"^\s*(-\s*)?run:\s*make ci\s*$", text, re.M) is not None,
        "ci.yml has no `run: make ci` step",
    )
    for v in ("3.11", "3.12"):
        need(
            re.search(rf"python-version:.*['\"]?{re.escape(v)}['\"]?", text) is not None
            or re.search(rf"-\s*['\"]{re.escape(v)}['\"]", text) is not None,
            f"ci.yml matrix lacks Python {v}",
        )
    need("matrix.python-version" in text, "ci.yml does not use matrix.python-version")
    makefile = rel(ctx, "Makefile").read_text(encoding="utf-8")
    m = re.search(r"^ci:([^\n]*)\n((?:\t[^\n]*\n)*)", makefile, re.M)
    need(m is not None, "Makefile has no ci target")
    assert m is not None
    body = m.group(1) + " " + m.group(2)
    for part in ("lint", "typecheck", "test", "demo"):
        need(re.search(rf"\b{part}\b", body) is not None, f"make ci does not run {part}")
    if ctx.fast:
        parts = {
            "G1 tests": ctx.results.get("test_ok", False),
            "G2 static": ctx.results.get("static_ok", False),
            "G4 demo": ctx.results.get("demo_ok", False),
        }
        failing = [k for k, ok in parts.items() if not ok]
        need(not failing, f"workflow ok; fast: {', '.join(failing)} failing")
        return "workflow ok (3.11, 3.12); fast: make ci not run, components pass"
    ci = ctx.make("ci", timeout=3600)
    need(ci.ok, f"make ci failed (exit {ci.code}): {ci.tail()}")
    return f"workflow runs make ci on 3.11 and 3.12; make ci passed in {ci.secs:.0f}s"


def g4(ctx: Ctx) -> str:
    """Offline demo: under 3 minutes, no network, no model calls, golden match."""
    golden = rel(ctx, DEMO_GOLDEN)
    need(bool(tracked(ctx, str(DEMO_GOLDEN))), f"{DEMO_GOLDEN} not committed")
    ledger = rel(ctx, "artifacts/usage/ledger.jsonl")
    before = sha256_file(ledger) if ledger.exists() else None
    out = ctx.tmp / "demo_output.txt"
    prefix = network_isolation()
    proc = ctx.run(
        [*prefix, "make", "--no-print-directory", "demo", f"DEMO_OUT={out}"],
        "make-demo",
        timeout=DEMO_LIMIT_S + 30,
        extra_env={**offline_env(), "BASERATE_NO_MODEL_CALLS": "1"},
    )
    isolation = "netns" if prefix else "dead-proxy"
    need(proc.ok, f"make demo failed (exit {proc.code}, {isolation}): {proc.tail()}")
    need(proc.secs < DEMO_LIMIT_S, f"make demo took {proc.secs:.0f}s >= {DEMO_LIMIT_S:.0f}s")
    after = sha256_file(ledger) if ledger.exists() else None
    need(before == after, "usage ledger changed during demo (model call)")
    need(out.is_file(), "demo wrote no output to DEMO_OUT")
    need(out.read_bytes() == golden.read_bytes(), f"demo output differs from {DEMO_GOLDEN}")
    ctx.results["demo_ok"] = True
    return f"demo {proc.secs:.0f}s offline ({isolation}), matches {DEMO_GOLDEN}"


E0_FIELDS = (
    ("estimators", "estimator"),
    ("prevalences", "prevalence"),
    ("budgets_per_10k", "budget_per_10k"),
    ("sample_sizes", "sample_size"),
    ("score_distributions", "score_distribution"),
)
E0_RESAMPLING = frozenset({"cluster_bootstrap", "bayesian_bootstrap", "composite"})


def _e0_norm(value: Any) -> str:
    """Canonical text of a grid value (numbers by value, so 1e-3 equals 0.001)."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int | float):
        return f"{float(value):g}"
    return str(value)


def e0_cell_id(estimator: str, dist: str, design: str, prevalence: float, budget: float) -> str:
    """The id tools/run_e0.py gives a cell."""
    return f"{estimator}|{dist}|{design}|p{prevalence:g}|b{budget:g}"


def closest_design(designs: dict[str, Any], n_neg: int, n_pos: int, clustered: bool) -> str:
    """The E0 design an E2 estimate relies on (the frozen headline rule of prereg 0001)."""
    pool = [
        (name, d) for name, d in designs.items() if (int(d["neg_cluster_size"]) > 1) == clustered
    ]
    need(bool(pool), f"no E0 design with clustered={clustered}")

    def dist(d: dict[str, Any]) -> float:
        nn = int(d["neg_clusters"]) * int(d["neg_cluster_size"])
        return abs(math.log(nn) - math.log(n_neg)) + abs(
            math.log(int(d["n_pos"])) - math.log(n_pos)
        )

    return min(pool, key=lambda item: (dist(item[1]), item[0]))[0]


def count_hits(b64: str, reps: int) -> int:
    """Covered replicates in a bit-packed (numpy packbits, MSB first) hit record."""
    data = base64.b64decode(b64)
    need(len(data) == (reps + 7) // 8, "hit record length does not match replicates")
    full, rem = divmod(reps, 8)
    total = sum(bin(x).count("1") for x in data[:full])
    if rem:
        need(data[full] & ((1 << (8 - rem)) - 1) == 0, "hit record has bits past replicates")
        total += bin(data[full] >> (8 - rem)).count("1")
    return total


def g5(ctx: Ctx) -> str:
    """Statistical engine: E0 coverage on the frozen grid, recounted from banked hits."""
    art_rel = "artifacts/e0/summary.json"
    summary = load_json(rel(ctx, art_rel))
    config = check_prereg_link(ctx, art_rel, summary)
    grid = config.get("grid", {})
    need(
        all(isinstance(grid.get(k), list) and grid[k] for k, _ in E0_FIELDS),
        "prereg grid needs estimators, prevalences, budgets_per_10k, sample_sizes, "
        "score_distributions",
    )
    nominal = float(config.get("nominal", 0.95))
    tol = float(config.get("tolerance", E0_TOLERANCE))
    need(tol <= E0_TOLERANCE, f"E0 tolerance {tol} is looser than {E0_TOLERANCE}")
    expected = set(product(*(tuple(_e0_norm(v) for v in grid[k]) for k, _ in E0_FIELDS)))
    seen: set[tuple[str, ...]] = set()
    passes: dict[str, bool] = {}
    failing: list[str] = []
    for cell in summary.get("cells", []):
        key = tuple(_e0_norm(cell[f]) for _, f in E0_FIELDS)
        need(key not in seen, f"duplicate E0 cell {cell.get('id')}")
        seen.add(key)
        reps = int(cell["replicates"])
        method = str(cell["estimator"]).split(":", 1)[-1]
        floor = E0_MIN_REPLICATES_BOOTSTRAP if method in E0_RESAMPLING else E0_MIN_REPLICATES
        need(reps >= floor, f"cell {cell.get('id')} has {reps} replicates < {floor}")
        hits = count_hits(str(cell["hits_b64"]), reps)
        need(hits == int(cell["covered"]), f"cell {cell.get('id')} covered count does not recount")
        cov = hits / reps
        mcse = math.sqrt(cov * (1 - cov) / reps)
        ok = cov >= nominal - tol - 2 * mcse
        need(bool(cell.get("passes")) == ok, f"cell {cell.get('id')} pass flag disagrees")
        passes[str(cell["id"])] = ok
        if not ok:
            failing.append(str(cell["id"]))
    need(not (expected - seen), f"{len(expected - seen)} preregistered cells missing")
    need(not (seen - expected), f"{len(seen - expected)} cells outside the preregistered grid")
    e2_path = rel(ctx, "artifacts/e2/results.json")
    need(e2_path.is_file(), "no E2 results: cannot check the cells E2 headline intervals rely on")
    headline = [e for e in load_json(e2_path).get("estimates", []) if e.get("headline")]
    need(bool(headline), "E2 results mark no headline estimates")
    designs = config.get("designs", {})
    relied: set[str] = set()
    for e in headline:
        design = closest_design(designs, int(e["n_neg"]), int(e["n_pos"]), bool(e["clustered"]))
        for dist in grid["score_distributions"]:
            relied.add(
                e0_cell_id(
                    f"budget_recall:{e['method']}",
                    dist,
                    design,
                    float(e["prevalence"]),
                    float(e["budget"]),
                )
            )
    unknown = sorted(c for c in relied if c not in passes)
    need(not unknown, f"E2 headline relies on cells E0 did not run: {', '.join(unknown[:3])}")
    bad = sorted(c for c in relied if not passes[c])
    need(not bad, f"E2 headline cells fail: {', '.join(bad[:3])}")
    if failing:
        unsupported = rel(ctx, "src/baserate/estimate/unsupported_cells.json")
        data = load_json(unsupported) if unsupported.exists() else {}
        listed = (
            {str(c.get("id")) for c in data.get("cells", [])} if isinstance(data, dict) else set()
        )
        need(set(failing) <= listed, "failing cells lack a library warning entry")
        docs = rel(ctx, "docs/UNSUPPORTED.md")
        doc_text = docs.read_text(encoding="utf-8") if docs.exists() else ""
        need(all(c in doc_text for c in failing), "failing cells not listed in docs/UNSUPPORTED.md")
    plots = summary.get("plots", [])
    need(
        bool(plots)
        and all(rel(ctx, p).is_file() and rel(ctx, p).stat().st_size > 0 for p in plots),
        "E0 plots missing",
    )
    return (
        f"{len(seen)} cells recounted from banked hits, {len(failing)} below criterion "
        f"(documented), {len(relied)} cells behind E2 headlines pass, {len(plots)} plots"
    )


def iter_shard(path: Path) -> Iterable[dict[str, Any]]:
    """Records of a gzip JSONL shard."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


CANARY_GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b")


def corpus_canary(ctx: Ctx) -> str:
    """The CANARY constant of tools/build_corpus.py in the clone; it must hold a GUID."""
    path = rel(ctx, "tools/build_corpus.py")
    need(path.is_file(), "tools/build_corpus.py missing")
    source = path.read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "CANARY" for t in node.targets
        ):
            value = ast.literal_eval(node.value)
            need(
                isinstance(value, str) and CANARY_GUID.search(value) is not None,
                "CANARY in tools/build_corpus.py holds no version-4 GUID",
            )
            return str(value)
    raise GateError("tools/build_corpus.py defines no CANARY")


def g6(ctx: Ctx) -> str:
    """Corpus: E1 targets, label audit, datasheet, tables state synthetic share."""
    manifest = load_json(rel(ctx, "artifacts/corpus/manifest.json"))
    counts = {"benign": 0, "benign_real": 0, "violations": 0}
    vtypes: set[str] = set()
    runs: set[str] = set()
    synthetic = 0
    shards = manifest.get("shards", [])
    need(bool(shards), "manifest lists no shards")
    canary = manifest.get("canary")
    need(isinstance(canary, str) and bool(canary), "manifest has no canary string (brief rule 9)")
    need(
        canary == corpus_canary(ctx), "manifest canary differs from CANARY in tools/build_corpus.py"
    )
    paths = [str(s["path"]) for s in shards]
    need(len(paths) == len(set(paths)), "manifest lists a shard more than once")
    seen_ids: set[str] = set()
    for shard in shards:
        p = rel(ctx, shard["path"])
        need(p.is_file(), f"shard missing: {shard['path']}")
        need(p.stat().st_size < SHARD_LIMIT_BYTES, f"shard over 50 MB: {shard['path']}")
        need(sha256_file(p) == shard["sha256"], f"hash mismatch: {shard['path']}")
        for t in iter_shard(p):
            need(str(t["trace_id"]) not in seen_ids, f"duplicate trace_id {t['trace_id']}")
            need(t.get("canary") == canary, f"trace {t['trace_id']} lacks the corpus canary")
            seen_ids.add(str(t["trace_id"]))
            runs.add(str(t["source_run_id"]))
            procedural = t.get("source") == "procedural"
            need(
                not procedural or t.get("synthetic") is True,
                f"procedural trace {t.get('trace_id')} not labeled synthetic",
            )
            synthetic += bool(t.get("synthetic"))
            if t["label"] == "benign":
                counts["benign"] += 1
                counts["benign_real"] += t.get("source") == "real_agent"
            elif t["label"] == "violation":
                step = t.get("first_violating_step")
                need(
                    isinstance(step, int) and 0 <= step < len(t.get("actions", [])),
                    f"violation {t.get('trace_id')} lacks a valid first_violating_step",
                )
                counts["violations"] += 1
                vtypes.add(str(t["violation_type"]))
    total = counts["benign"] + counts["violations"]
    short = [f"{k} {counts[k]} < {E1_TARGETS[k]}" for k in counts if counts[k] < E1_TARGETS[k]]
    if len(vtypes) < E1_TARGETS["violation_types"]:
        short.append(f"violation types {len(vtypes)} < {E1_TARGETS['violation_types']}")
    need(not short, "; ".join(short))
    audit = load_json(rel(ctx, "artifacts/corpus/label_audit.json"))
    items = audit.get("items", [])
    need(bool(items), "label audit has no items")
    agree = sum(
        1
        for i in items
        if i["audit_label"] == i["original_label"]
        and i.get("audit_type") == i.get("original_type")
        and i.get("audit_step") == i.get("original_step")
    )
    need(
        math.isclose(agree / len(items), float(audit["agreement"]), abs_tol=1e-9),
        "label_audit agreement does not recompute",
    )
    data_md = rel(ctx, "docs/DATA.md")
    need(data_md.is_file(), "docs/DATA.md missing")
    visible = re.sub(r"<!--.*?-->", "", data_md.read_text(encoding="utf-8"), flags=re.S)
    need(canary in visible, "docs/DATA.md does not state the canary")
    dtext = data_md.read_text(encoding="utf-8").lower()
    gaps = [s for s in DATASHEET_SECTIONS if s not in dtext]
    need(not gaps, f"datasheet lacks: {', '.join(gaps)}")
    need("agreement" in dtext, "docs/DATA.md does not report label-audit agreement")
    tables = sorted((ctx.clone / "artifacts" / "report" / "tables").glob("*.md"))
    need(bool(tables), "no generated tables under artifacts/report/tables")
    for tbl in tables:
        tt = tbl.read_text(encoding="utf-8").lower()
        need(
            "synthetic share" in tt and "independent source runs" in tt,
            f"{tbl.name} does not state synthetic share and independent source runs",
        )
    return (
        f"{counts['benign']} benign ({counts['benign_real']} real), {counts['violations']} "
        f"violations, {len(vtypes)} types, synthetic share {synthetic / total:.3f}, "
        f"{len(runs)} source runs, audit agreement {agree / len(items):.3f}"
    )


def e2_missing(config: dict[str, Any], res: dict[str, Any]) -> list[str]:
    """What prereg/0003's config commits the E2 results to that the results file lacks.

    Every preregistered monitor on each of its sets needs every prevalence, budget, and
    metric, made with the preregistered headline method; the preregistered
    fixed-threshold, time-to-detection, and size-ladder monitors need their rows; and
    every monitor on the primary set needs its breakdowns by violation source, violation
    type, and subject model (prereg/0003 restricts them to no family).
    """
    method = (config.get("headline") or {}).get("method")
    have = {
        (
            e.get("monitor"),
            e.get("set"),
            float(e["prevalence"]),
            int(e["budget"]),
            e.get("metric"),
            e.get("method"),
        )
        for e in res.get("estimates", [])
    }
    monitors = [m for m in config.get("monitors", []) if isinstance(m, dict)]
    missing = [
        f"estimate {m['id']} {s} p{float(p):g} b{int(b)} {metric} ({method})"
        for m in monitors
        for s in m.get("sets", [])
        for p in config.get("prevalences", [])
        for b in config.get("budgets_per_10k", [])
        for metric in E2_PREREG_METRICS
        if (m["id"], s, float(p), int(b), metric, method) not in have
    ]
    fixed = {(r.get("monitor"), r.get("set")) for r in res.get("fixed_threshold", [])}
    missing += [
        f"fixed-threshold rows for {m}"
        for m in (config.get("fixed_threshold") or {}).get("monitors", [])
        if (m, "primary") not in fixed
    ]
    ttd = {r.get("monitor") for r in res.get("time_to_detection", [])}
    missing += [
        f"time to detection for {m}"
        for m in (config.get("time_to_detection") or {}).get("monitors", [])
        if m not in ttd
    ]
    groups = {(r.get("monitor"), r.get("by")) for r in res.get("breakdowns", [])}
    missing += [
        f"breakdown by {g} for {m['id']}"
        for m in monitors
        if "primary" in m.get("sets", [])
        for g in E2_BREAKDOWN_GROUPS
        if (m["id"], g) not in groups
    ]
    rungs = {r.get("monitor") for r in (res.get("size_ladder") or {}).get("rungs", [])}
    missing += [
        f"size-ladder rung for {m['id']}"
        for m in monitors
        if m.get("family") == "model" and m["id"] not in rungs
    ]
    return missing


def expected_hypotheses(exp: str, config: dict[str, Any]) -> list[str]:
    """Hypothesis ids a preregistration commits its results to, named as tools/analyze.py does.

    E3: H3.1 and H3.2 per monitor and H3.3 per monitor and target prevalence, the monitor
    named without its family prefix; E4: H4.1 and H4.2 per cascade pair index.
    """
    if exp == "e3":
        ids = []
        for mon in config.get("monitors", []):
            short = str(mon["id"] if isinstance(mon, dict) else mon).split(":", 1)[-1]
            for kind in config.get("hypotheses", ["H3.1", "H3.2", "H3.3"]):
                if kind == "H3.3":
                    ids += [f"H3.3-{short}-p{float(t):g}" for t in config["target_prevalences"]]
                else:
                    ids.append(f"{kind}-{short}")
        return ids
    return [
        f"{kind}-{pair['index']}"
        for pair in config.get("pairs", [])
        for kind in config.get("hypotheses", ["H4.1", "H4.2"])
    ]


def g7(ctx: Ctx) -> str:
    """Main study: E2 as preregistered, with intervals, from artifacts."""
    art_rel = "artifacts/e2/results.json"
    res = load_json(rel(ctx, art_rel))
    need(res.get("confirmatory") is True, "E2 results not marked confirmatory")
    config = check_prereg_link(ctx, art_rel, res)
    families = {e["family"] for e in res.get("estimates", [])}
    need(len(families) >= E2_MIN_FAMILIES, f"{len(families)} monitor families < {E2_MIN_FAMILIES}")
    need(
        set(map(float, config.get("prevalences", []))) >= set(E2_PREVALENCES),
        "preregistered prevalences do not include 1e-2, 1e-3, 1e-4",
    )
    need(
        set(map(int, config.get("budgets_per_10k", []))) >= set(E2_BUDGETS),
        "preregistered budgets do not include 5, 20, 100",
    )
    by_monitor: dict[str, set[tuple[float, int, str]]] = {}
    for e in res["estimates"]:
        lo, est, hi = float(e["ci_low"]), float(e["estimate"]), float(e["ci_high"])
        need(lo <= est <= hi, f"interval does not contain estimate for {e.get('monitor')}")
        need(int(e.get("n_independent_runs", 0)) > 0, "estimate lacks independent run count")
        by_monitor.setdefault(e["monitor"], set()).add(
            (float(e["prevalence"]), int(e["budget"]), str(e["metric"]))
        )
    grid = set(product(E2_PREVALENCES, E2_BUDGETS, E2_METRICS))
    incomplete = [m for m, cells in by_monitor.items() if not grid <= cells]
    need(not incomplete, f"monitors missing grid cells: {', '.join(incomplete[:4])}")
    scores = rel(ctx, str(res.get("scores_file", "")))
    need(
        scores.is_file() and sha256_file(scores) == res.get("scores_sha256"),
        "E2 scores file missing or hash mismatch",
    )
    missing = e2_missing(config, res)
    more = f" (and {len(missing) - 1} more)" if len(missing) > 1 else ""
    need(
        not missing,
        f"E2 lacks what its preregistration plans: {missing[0] if missing else ''}{more}",
    )
    return (
        f"{len(by_monitor)} monitors in {len(families)} families, full grid, intervals present; "
        "every preregistered monitor, set, and analysis present"
    )


def g8(ctx: Ctx) -> str:
    """Transfer and cascades: E3 and E4 ran and every hypothesis is reported."""
    report = rel(ctx, "docs/REPORT.md")
    need(report.is_file(), "docs/REPORT.md missing")
    rtext = report.read_text(encoding="utf-8")
    parts = []
    for exp in ("e3", "e4"):
        art_rel = f"artifacts/{exp}/results.json"
        res = load_json(rel(ctx, art_rel))
        config = check_prereg_link(ctx, art_rel, res)
        hyps = res.get("hypotheses", [])
        need(bool(hyps), f"{exp.upper()} lists no hypotheses")
        listed = {str(h.get("id")) for h in hyps}
        absent = [i for i in expected_hypotheses(exp, config) if i not in listed]
        need(
            not absent,
            f"{exp.upper()} lacks preregistered hypotheses: {', '.join(absent[:3])}",
        )
        for h in hyps:
            need(
                h.get("outcome") in {"supported", "not_supported", "inconclusive"},
                f"{exp.upper()} {h.get('id')} has no outcome",
            )
            need(str(h["id"]) in rtext, f"{exp.upper()} {h['id']} not reported in REPORT.md")
        nulls = sum(1 for h in hyps if h["outcome"] != "supported")
        parts.append(f"{exp.upper()} {len(hyps)} hypotheses ({nulls} null or inconclusive)")
    return "; ".join(parts) + " reported"


def g9(ctx: Ctx) -> str:
    """Usable by others: pip install -e, three offline examples, Armour adapter tested."""
    readme = rel(ctx, "README.md").read_text(encoding="utf-8")
    missing = [str(e) for e in EXAMPLES if not rel(ctx, e).is_file()]
    need(not missing, f"examples missing: {', '.join(missing)}")
    undocumented = [str(e) for e in EXAMPLES if str(e) not in readme]
    need(not undocumented, f"examples not documented in README: {', '.join(undocumented)}")
    if ctx.fast:
        python = ["uv", "run", "--frozen", "python"]
        how = "uv env (fast)"
    else:
        venv = ctx.tmp / "pipvenv"
        mk = ctx.run([sys.executable, "-m", "venv", str(venv)], "venv", timeout=300)
        need(mk.ok, f"cannot create venv: {mk.tail()}")
        pip = ctx.run(
            [str(venv / "bin" / "pip"), "install", "-q", "-e", str(ctx.clone)],
            "pip-install",
            timeout=1200,
        )
        need(pip.ok, f"pip install -e . failed: {pip.tail()}")
        python = [str(venv / "bin" / "python")]
        how = "pip install -e ok"
    prefix = network_isolation()
    for ex in EXAMPLES:
        proc = ctx.run(
            [*prefix, *python, str(ex)],
            f"example-{ex.stem}",
            timeout=600,
            extra_env={**offline_env(), "BASERATE_NO_MODEL_CALLS": "1"},
        )
        need(proc.ok, f"{ex} failed offline: {proc.tail()}")
    tests = [t for t in tracked(ctx, "tests/*armour*.py") if Path(t).name.startswith("test_")]
    need(bool(tests), "no Armour adapter test (tests/test_*armour*.py)")
    stems = {"tests." + Path(t).with_suffix("").name for t in tests}
    cases = [
        o
        for c, o in junit_cases(ctx.results.get("junit", Path("/nonexistent")))
        if any(c == s or c.startswith(s + ".") for s in stems)
    ]
    need(bool(cases) and all(o == "passed" for o in cases), "Armour adapter tests did not pass")
    return f"{how}; 3 examples ran offline; {len(cases)} Armour adapter tests passed"


def g10(ctx: Ctx) -> str:
    """Claim integrity: claimcheck and freshness on README.md and docs/REPORT.md."""
    need(rel(ctx, "docs/REPORT.md").is_file(), "docs/REPORT.md missing")
    makefile = rel(ctx, "Makefile").read_text(encoding="utf-8")
    for target in ("claimcheck", "freshness"):
        m = re.search(rf"^{target}:[^\n]*\n((?:\t[^\n]*\n)*)", makefile, re.M)
        need(
            m is not None and "README.md" in m.group(1) and "docs/REPORT.md" in m.group(1),
            f"make {target} does not cover README.md and docs/REPORT.md",
        )
    cc = ctx.make("claimcheck", timeout=600)
    need(cc.ok, f"make claimcheck failed: {cc.tail()}")
    numbers_path = rel(ctx, "artifacts/report/numbers.json")
    if numbers_path.is_file():
        sources = load_json(numbers_path).get("sources")
        need(isinstance(sources, dict) and bool(sources), "numbers.json records no sources")
        stale = [
            src
            for src, digest in sources.items()
            if not rel(ctx, src).is_file() or sha256_file(rel(ctx, src)) != digest
        ]
        need(not stale, f"numbers.json was built from older artifacts: {', '.join(stale)}")
    if ctx.fast:
        return f"claimcheck passed ({cc.tail()}); fast: freshness and reproduce not run"
    fr = ctx.make("freshness", timeout=3600)
    need(fr.ok, f"make freshness failed: {fr.tail()}")
    rp = ctx.make("reproduce", timeout=7200)
    rstat = rp.status("REPRODUCE:")
    need(
        rp.ok and rstat.startswith("REPRODUCE: PASS"),
        f"make reproduce failed: {rstat or rp.tail()}",
    )
    return f"claimcheck ({cc.tail()}), freshness ({fr.tail()}), and reproduce passed ({rstat})"


def g11(ctx: Ctx) -> str:
    """Preregistration integrity: frozen files unchanged, artifacts cite earlier preregs."""
    need(
        git(ctx.clone, "rev-parse", "--is-shallow-repository") != "true",
        "git history is shallow; run git fetch --unshallow",
    )
    meta = {"prereg/README.md", "prereg/.gitkeep"}
    preregs = [p for p in tracked(ctx, "prereg/*.md") if p not in meta]
    need(bool(preregs), "no preregistrations under prereg/")
    ever = git(
        ctx.clone,
        "log",
        "--full-history",
        "--diff-merges=separate",
        "--no-renames",
        "--format=",
        "--name-only",
        "-z",
        "--",
        "prereg/",
    )
    gone = {p.strip("\n") for p in ever.split("\0") if p.strip("\n")} - set(tracked(ctx, "prereg/"))
    need(not (gone - meta), "a preregistration file was deleted")
    drafted = []
    freezes: dict[str, Freeze] = {}
    for p in preregs:
        f = freeze_of(ctx.clone, p)
        freezes[p] = f
        need(
            all(f.states[c] is None or f.states[c][1] in REGULAR_FILE_MODES for c in f.introduced),
            f"{p} is not a regular file",
        )
        need(
            f.commit is not None, f"{p} is still a draft (its config holds a TO_BE_SET placeholder)"
        )
        assert f.commit is not None
        need(
            len(repo_history(ctx.clone)[1][f.commit]) <= 1,
            f"{p} was frozen in a merge commit",
        )
        need(
            len(f.introduced) == len(f.drafts) + 1,
            f"{p} changed after it was frozen ({len(f.introduced)} commits)",
        )
        need(
            all(f.states[c] == f.states[f.commit] for c in descendant_set(ctx.clone, f.commit)),
            f"{p} changed after it was frozen (a later commit holds other content)",
        )
        final = version_config(ctx.clone, f.states[f.commit])
        for d in f.drafts:
            need(
                is_strict_ancestor(ctx.clone, d, f.commit),
                f"{p}: draft {d[:12]} does not precede its freezing commit",
            )
            need(
                keeps_fixed_values(version_config(ctx.clone, f.states[d]), final),
                f"{p}: its freezing commit changes a value that draft {d[:12]} had fixed",
            )
        need(
            git(ctx.clone, "rev-parse", f"HEAD:{p}") == f.states[f.commit][0],
            f"{p} at HEAD differs from its freezing commit",
        )
        if f.drafts:
            drafted.append(p.removeprefix("prereg/").split("-", 1)[0])
    for p in preregs:
        front = rel(ctx, p).read_text(encoding="utf-8").split("\n---", 1)[0]
        m = re.search(r"^amends:[ \t]*['\"]?(prereg/[^'\"\s]+)", front, re.M)
        if m is None:
            continue
        original = freezes.get(m.group(1))
        need(original is not None, f"{p} amends {m.group(1)}, which is not a preregistration")
        assert original is not None and original.commit is not None
        after = descendant_set(ctx.clone, original.commit)
        need(
            bool(freezes[p].introduced) and set(freezes[p].introduced) <= after,
            f"{p} amends {m.group(1)}, which was not frozen before {p} was first committed",
        )
    confirmatory = []
    for art in tracked(ctx, "artifacts/*.json"):
        if art.startswith("artifacts/review/"):
            continue
        try:
            data = json.loads(rel(ctx, art).read_text(encoding="utf-8"))
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("confirmatory") is True or "prereg" in data:
            check_prereg_link(ctx, art, data)
        if data.get("confirmatory") is True:
            confirmatory.append(art)
    need(bool(confirmatory), "no confirmatory artifacts yet")
    pv = ctx.make("prereg-verify", timeout=600)
    need(pv.ok, f"make prereg-verify failed: {pv.tail()}")
    after_draft = f"; frozen after a declared draft: {', '.join(drafted)}" if drafted else ""
    return (
        f"{len(preregs)} preregistrations frozen; "
        f"{len(confirmatory)} confirmatory artifacts cite earlier preregs{after_draft}"
    )


def g12(ctx: Ctx) -> str:
    """Independent review: latest review is SHIP for the current code."""
    review = load_json(rel(ctx, "artifacts/review/latest.json"))
    need(review.get("verdict") == "SHIP", f"latest review verdict {review.get('verdict')}")
    need(review.get("blocking") == [], "latest review has open blocking issues")
    reviewed = str(review.get("reviewed_commit", ""))
    head = git(ctx.clone, "rev-parse", "HEAD")
    need(
        reviewed == head or is_strict_ancestor(ctx.clone, reviewed, head),
        "reviewed commit is not HEAD or an ancestor",
    )
    changed = git(ctx.clone, "diff", "--name-only", reviewed, head).splitlines()
    drift = [p for p in changed if not drift_allowed(p)]
    need(not drift, f"code changed since review: {', '.join(drift[:5])}")
    log = rel(ctx, "docs/REVIEW_LOG.md")
    log_text = log.read_text(encoding="utf-8") if log.is_file() else ""
    unlisted = [
        str(i.get("id"))
        for i in review.get("non_blocking", [])
        if not id_logged(str(i.get("id")), log_text)
    ]
    need(
        not unlisted, f"non-blocking issues missing from docs/REVIEW_LOG.md: {', '.join(unlisted)}"
    )
    proc = ctx.run(
        [sys.executable, "tools/review.py", "--validate-only", "--root", str(ctx.clone)],
        "review-validate",
        timeout=300,
    )
    verdict = proc.status("REVIEW:")
    need(
        proc.ok and verdict == "REVIEW: SHIP",
        f"make review validation: {verdict or proc.tail()}",
    )
    n_logged = len(review.get("non_blocking", []))
    return f"REVIEW: SHIP for {reviewed[:12]}; {n_logged} non-blocking logged"


MARKER_RE = re.compile(r"<!--\s*@(?:prose|table|references)\b[^>]*-->")
INTERVAL_RE = re.compile(r"\[\s*-?\d[\d.,]*\s*,\s*-?\d")


def leftover_markers(text: str) -> list[str]:
    """Template markers left in a rendered document (unwritten prose or tables)."""
    return MARKER_RE.findall(text)


def has_finding_sentence(first_screen: str) -> bool:
    """Whether a prose line (not a table, caption, image, heading, or code) holds an interval."""
    in_code = False
    for line in first_screen.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        text = line.strip()
        if in_code or not text or text[0] in "|*!#<":
            continue
        if INTERVAL_RE.search(text):
            return True
    return False


def section_has_text(body: str) -> bool:
    """Whether a section holds anything besides HTML comments and whitespace."""
    return bool(re.sub(r"<!--.*?-->", "", body, flags=re.S).strip())


def g13(ctx: Ctx) -> str:
    """Documentation: README per section 11, REPORT complete, citations verified."""
    readme = rel(ctx, "README.md").read_text(encoding="utf-8")
    need(BRIEF_MARKER not in readme, "README.md is still the build brief")
    report_p = rel(ctx, "docs/REPORT.md")
    need(report_p.is_file(), "docs/REPORT.md missing")
    report = report_p.read_text(encoding="utf-8")
    for name, text in (("README.md", readme), ("docs/REPORT.md", report)):
        need(EM_DASH not in text, f"{name} contains an em dash")
        low = text.lower()
        hype = [w for w in HYPE_WORDS if w in low]
        need(not hype, f"{name} uses hype words: {', '.join(hype)}")
    sections = md_sections(readme)
    need(bool(sections), "README.md has no sections")
    first_screen = readme[: readme.find("\n## " + sections[0][0])] if sections else readme
    need(
        re.match(r"#\s+base-rate\b", readme.lstrip()) is not None,
        "README must open with # base-rate",
    )
    for name, text in (("README.md", readme), ("docs/REPORT.md", report)):
        left = leftover_markers(text)
        need(not left, f"{name} still holds template markers: {', '.join(left[:3])}")
    need(
        has_finding_sentence(first_screen),
        "first screen lacks a one-sentence finding with its interval",
    )
    svgs = re.findall(r"!\[[^\]]*\]\(([^)]+\.svg)\)", first_screen)
    need(bool(svgs), "first screen lacks the headline SVG figure")
    report_manifest = rel(ctx, "artifacts/report/manifest.json")
    generated = (
        set(load_json(report_manifest).get("figures", [])) if report_manifest.exists() else set()
    )
    need(
        all(rel(ctx, s).is_file() and s in generated for s in svgs),
        "headline SVG missing or not generated by make report",
    )
    need(re.search(r"^\|.+\|\s*$", first_screen, re.M) is not None, "first screen lacks a table")
    need(
        re.search(r"quickstart", first_screen, re.I) is not None and "```" in first_screen,
        "first screen lacks a quickstart code block",
    )
    heads = [h.lower() for h, _ in sections]
    pos = -1
    for key, label in README_SECTIONS:
        idx = next((i for i, h in enumerate(heads) if key in h and i > pos), None)
        need(idx is not None, f"README lacks section: {label} (or out of order)")
        assert idx is not None
        pos = idx
    limits = next(b for h, b in sections if "limitation" in h.lower())
    need(section_has_text(limits), "README limitations section has no text")
    results_body = next(b for h, b in sections if "key results" in h.lower())
    bullets = [ln for ln in results_body.splitlines() if re.match(r"\s*[-*]\s", ln)]
    need(bool(bullets), "key results section has no result bullets")
    unlinked = [b for b in bullets if "artifacts/" not in b or "prereg/" not in b]
    need(not unlinked, "a key result lacks links to its artifact and preregistration")
    how = next(b for h, b in sections if "how it works" in h.lower())
    need(
        "```mermaid" in how or re.search(r"!\[[^\]]*\]\(", how) is not None,
        "how it works has no diagram",
    )
    rheads = [h.lower() for h, _ in md_sections(report)]
    rmissing = [s for s in REPORT_SECTIONS if not any(s in h for h in rheads)]
    need(not rmissing, f"REPORT.md lacks sections: {', '.join(rmissing)}")
    words = word_count(report)
    need(REPORT_WORDS[0] <= words <= REPORT_WORDS[1], f"REPORT.md has {words} words")
    figs = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", report)
    need(bool(figs), "REPORT.md has no figures")
    base = report_p.parent
    for fig in figs:
        target = (base / fig).resolve()
        need(target.is_file(), f"REPORT.md figure missing: {fig}")
        rel_fig = str(target.relative_to(ctx.clone.resolve()))
        need(rel_fig in generated, f"REPORT.md figure not generated by make report: {fig}")
    cc = ctx.make("citecheck", timeout=600)
    m = re.search(r"CITECHECK: PASS (\d+)/(\d+)", cc.status("CITECHECK:"))
    need(
        cc.ok and m is not None and m.group(1) == m.group(2) and int(m.group(1)) > 0,
        f"make citecheck: {cc.status('CITECHECK:') or cc.tail() or 'failed'}",
    )
    assert m is not None
    return (
        f"README per section 11; REPORT {words} words; citecheck {m.group(1)}/{m.group(2)} verified"
    )


def g14(ctx: Ctx) -> str:
    """Hygiene: secrets, budget, private/, required files, clean original tree."""
    orig = ctx.results["orig"]
    problems = [v for v in orig.values() if v]
    for name in ("LICENSE", "CITATION.cff", "SECURITY.md", "CONTRIBUTING.md"):
        if not tracked(ctx, name):
            problems.append(f"{name} missing")
    caps = tomllib.loads(rel(ctx, "budget.toml").read_text(encoding="utf-8"))
    if caps.get("cash_usd") != 0:
        problems.append("budget.toml cash_usd is not 0")
    for key, default in DEFAULT_CAPS.items():
        value = caps.get(key)
        if not isinstance(value, int | float) or isinstance(value, bool):
            problems.append(f"budget.toml {key} missing")
        elif (value < default) if key in CAP_FLOORS else (value > default):
            problems.append(f"budget.toml {key} moved past its default")
    scan = ctx.make("secrets-scan", timeout=900)
    if not scan.ok:
        problems.append(f"secrets-scan: {scan.tail()}")
    bud = ctx.make("budget", timeout=300)
    lines = [ln.strip() for ln in bud.out.splitlines()]
    caps_line = bud.status("caps:")
    waived = re.fullmatch(r"caps: respected except waived overruns \(([^)]+)\)", caps_line)
    if not (bud.ok and "cash_usd = 0" in lines and (caps_line == "caps: all respected" or waived)):
        problems.append(f"make budget: {caps_line or bud.tail()}")
    ids = waived.group(1).split(", ") if waived else []
    if waived:
        problems.extend(waiver_problems(ctx, ids))
    need(not problems, "; ".join(problems))
    caps = (
        f"caps respected except waived overruns ({', '.join(ids)}, approved by Raj in DECISIONS.md)"
        if ids
        else "caps respected"
    )
    return (
        f"secrets-scan clean; cash_usd = 0, {caps}; private/ untracked; files present; tree clean"
    )


def waiver_problems(ctx: Ctx, ids: list[str]) -> list[str]:
    """Problems with the waivers ``make budget`` applied (G14).

    Each waiver must be tracked in budget_waivers.toml and cite a DECISIONS.md
    entry (``## <id> (...)``) that records Raj's approval on the waiver's
    approval date for that day and kind. ``make budget`` already checks that a
    waiver excuses exactly its one overrun and nothing else.
    """
    problems: list[str] = []
    if not tracked(ctx, "budget_waivers.toml"):
        return ["make budget applied waivers but budget_waivers.toml is not tracked"]
    data = tomllib.loads(rel(ctx, "budget_waivers.toml").read_text(encoding="utf-8"))
    waivers = {str(w.get("decision")): w for w in data.get("waiver", [])}
    decisions = rel(ctx, "DECISIONS.md")
    text = decisions.read_text(encoding="utf-8") if decisions.is_file() else ""
    for wid in ids:
        w = waivers.get(wid)
        if w is None:
            problems.append(f"waiver {wid} is not in budget_waivers.toml")
            continue
        m = re.search(rf"^## {re.escape(wid)} \(.*?(?=^## |\Z)", text, re.M | re.S)
        body = m.group(0) if m else ""
        approved = re.search(
            rf"approved by raj on {re.escape(str(w.get('approved_on')))}", body.lower()
        )
        if not (approved and str(w.get("day")) in body and str(w.get("kind")) in body):
            problems.append(f"waiver {wid} has no DECISIONS.md entry approved by Raj")
    return problems


GATE_FUNCS: dict[int, Callable[[Ctx], str]] = {
    1: g1,
    2: g2,
    4: g4,
    3: g3,
    5: g5,
    6: g6,
    7: g7,
    8: g8,
    9: g9,
    10: g10,
    11: g11,
    12: g12,
    13: g13,
    14: g14,
}
"""Gate order: G4 runs before G3 so the fast check can reuse the demo result."""


def run_gates(ctx: Ctx) -> list[Gate]:
    """Evaluate every gate; a crash in one gate fails only that gate."""
    out: dict[int, Gate] = {}
    for n, fn in GATE_FUNCS.items():
        try:
            evidence = fn(ctx)
            if ctx.fast and n in FAST_SKIPS and "fast" not in evidence:
                evidence += f"; fast: {FAST_SKIPS[n]}"
            out[n] = Gate(n, True, evidence)
        except GateError as exc:
            out[n] = Gate(n, False, str(exc))
        except Exception as exc:  # report, do not crash the whole check
            out[n] = Gate(n, False, f"error: {type(exc).__name__}: {exc}")
    return [out[n] for n in sorted(out)]


def main(argv: list[str] | None = None) -> int:
    """Run the ship check and print the gate table."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fast", action="store_true", help="skip slow steps; does not count")
    parser.add_argument("--keep", action="store_true", help="keep the temp dir (path on stderr)")
    args = parser.parse_args(argv)
    orig = Path(git(Path.cwd(), "rev-parse", "--show-toplevel"))
    head = git(orig, "rev-parse", "HEAD")
    orig_results = original_checks(orig)
    tmp = Path(tempfile.mkdtemp(prefix="baserate-shipcheck-"))
    clone = tmp / "clone"
    try:
        subprocess.run(
            ["git", "clone", "-q", "--no-hardlinks", str(orig), str(clone)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "-q", "--detach", head], cwd=clone, check=True, capture_output=True
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("VIRTUAL_ENV")}
        env.update({"BASERATE_NO_MODEL_CALLS": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        env.pop("UV_PROJECT_ENVIRONMENT", None)
        ctx = Ctx(orig=orig, clone=clone, tmp=tmp, fast=args.fast, env=env)
        ctx.results["orig"] = orig_results
        print(f"HEAD {head} fresh-clone", flush=True)
        gates = run_gates(ctx)
        for g in gates:
            print(f"G{g.n} {'PASS' if g.passed else 'FAIL'} {trunc(g.evidence)}", flush=True)
        passed = sum(g.passed for g in gates)
        if args.fast:
            print(f"SHIP-CHECK-FAST (does not count): {passed}/{GATES}")
        else:
            verdict = "PASS" if passed == GATES else "FAIL"
            print(f"SHIP-CHECK: {verdict} {passed}/{GATES}")
        return 0 if passed == GATES else 1
    finally:
        if args.keep:
            print(f"ship-check temp dir kept: {tmp}", file=sys.stderr)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
