"""Tests for tools/freshness.py and the render command of tools/claims.py.

claimcheck asks whether a claim value traces to some committed version of
its source; freshness asks whether it is the current one. A stale value
passes the first and must fail the second, so the control and the stale
cases below are the point of the gate (raise-v1 tests/mutation_test.py).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _helpers() -> ModuleType:
    if "integrity_helpers" in sys.modules:
        return sys.modules["integrity_helpers"]
    path = Path(__file__).parent / "fixtures" / "integrity" / "helpers.py"
    spec = importlib.util.spec_from_file_location("integrity_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["integrity_helpers"] = module
    spec.loader.exec_module(module)
    return module


h = _helpers()
claims = h.load_tool("claims")
claimcheck = h.load_tool("claimcheck")
freshness = h.load_tool("freshness")

DOCS = ("README.md", "docs/REPORT.md")
README_T = "docs/templates/README.md.tmpl"
ART = "artifacts/e2/results.json"
NEW_RECALL = ('"estimate": 0.4123', '"estimate": 0.4151')


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    h.isolate_git(monkeypatch)


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> Any:
    repo = h.Repo.init(tmp_path_factory.mktemp("fresh") / "base")
    repo.copy_in(h.CLAIMS_REPO, ".")
    repo.copy_in(h.REPO_ROOT / "docs" / "BUILD_BRIEF.md", "docs/BUILD_BRIEF.md")
    repo.copy_in(h.REPO_ROOT / "docs" / "BUILD_BRIEF.sha256", "docs/BUILD_BRIEF.sha256")
    repo.commit("bank fixture artifacts and templates")
    assert claims.main(["--root", str(repo.path), "render"]) == 0
    repo.commit("render documents")
    return repo


@pytest.fixture
def repo(base: Any, tmp_path: Path) -> Any:
    return base.copy_to(tmp_path / "repo")


def run(tool: ModuleType, repo: Any, capsys: pytest.CaptureFixture[str]) -> tuple[int, list[str]]:
    capsys.readouterr()
    code = tool.main(["--root", str(repo.path), *DOCS])
    return code, capsys.readouterr().out.splitlines()


def edit(repo: Any, rel: str, old: str, new: str) -> None:
    text = repo.read(rel)
    assert old in text, f"{old!r} not in {rel}"
    repo.write(rel, text.replace(old, new))


def test_control_passes_with_counts(repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(freshness, repo, capsys)
    assert code == 0, out
    assert out == ["FRESHNESS: PASS 12 live claims current in 2 documents"]


def test_cli_from_repo_root(repo: Any) -> None:
    proc = subprocess.run(
        [sys.executable, "-I", str(h.TOOLS / "freshness.py"), *DOCS],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.splitlines()[-1] == "FRESHNESS: PASS 12 live claims current in 2 documents"


def test_stale_value_that_claimcheck_still_passes(
    repo: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # 0.412 was banked and stays in history, so claimcheck has no grounds to
    # object. It is simply no longer the value. That gap is why this gate exists.
    edit(repo, ART, *NEW_RECALL)
    repo.commit("re-bank E2")
    assert run(claimcheck, repo, capsys)[0] == 0
    code, out = run(freshness, repo, capsys)
    assert code == 1
    stale = (
        "FAIL README.md:5: claim e2.rule.recall is stale: the document says '0.412', "
        "artifacts/e2/results.json now gives '0.415'"
    )
    assert stale in out
    assert out[-1] == "FRESHNESS: FAIL 4 stale or unresolvable"


def test_rerendering_makes_it_current_again(repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    edit(repo, ART, *NEW_RECALL)
    repo.commit("re-bank E2")
    assert claims.main(["--root", str(repo.path), "render"]) == 0
    repo.commit("re-render")
    assert run(freshness, repo, capsys)[0] == 0
    assert run(claimcheck, repo, capsys)[0] == 0
    assert "recall at budget was 0.415" in repo.read("README.md")


MUTATIONS: list[tuple[str, Callable[[Any], None], str]] = [
    (
        "work-tree-artifact-changed",
        lambda r: edit(r, ART, *NEW_RECALL),
        "claim e2.rule.recall is stale",
    ),
    (
        "stale-count-under-a-filter",
        lambda r: edit(
            r, ART, '"id": "H2", "outcome": "not_supported"', '"id": "H2", "outcome": "supported"'
        ),
        "claim e2.supported is stale: the document says '2', artifacts/e2/results.json now "
        "gives '3'",
    ),
    (
        "stale-length",
        lambda r: edit(
            r,
            ART,
            '{"id": "H3", "outcome": "supported"}',
            '{"id": "H3", "outcome": "supported"}, {"id": "H4", "outcome": "inconclusive"}',
        ),
        "claim e2.hypotheses is stale",
    ),
    (
        "artifact-deleted",
        lambda r: r.git("rm", "-q", ART),
        "claim e2.rule.recall: source artifacts/e2/results.json is missing",
    ),
    (
        "extractor-no-longer-resolves",
        lambda r: edit(r, ART, '"estimates":', '"rows":'),
        "cannot re-derive from artifacts/e2/results.json: no key 'estimates'",
    ),
    (
        "selector-became-ambiguous",
        lambda r: edit(r, ART, '"prevalence": 0.0001', '"prevalence": 0.001'),
        "matches 2 elements",
    ),
    (
        "artifact-no-longer-json",
        lambda r: r.write(ART, "{"),
        "artifacts/e2/results.json is not valid JSON",
    ),
    (
        "template-edited-not-rerendered",
        lambda r: edit(r, README_T, "test fixture", "test fixture with {{param:2}} parts"),
        "does not match its template docs/templates/README.md.tmpl:3",
    ),
    (
        "document-hand-edited",
        lambda r: edit(
            r, "README.md", "independent source runs.", "independent source runs (n=7)."
        ),
        "README.md:5:",
    ),
    (
        "claim-unregistered",
        lambda r: edit(r, README_T, "{{claim:e2.supported}}", "{{claim:e2.gone}}"),
        "claim 'e2.gone' is not registered",
    ),
    (
        "document-missing",
        lambda r: r.git("rm", "-q", "docs/REPORT.md"),
        "docs/REPORT.md: file not found",
    ),
    (
        "registry-missing",
        lambda r: r.git("rm", "-q", "docs/claims.json"),
        "docs/claims.json not found",
    ),
]


@pytest.mark.parametrize(
    ("mutate", "needle"), [m[1:] for m in MUTATIONS], ids=[m[0] for m in MUTATIONS]
)
def test_mutation_fails_freshness(
    repo: Any, capsys: pytest.CaptureFixture[str], mutate: Callable[[Any], None], needle: str
) -> None:
    mutate(repo)
    code, out = run(freshness, repo, capsys)
    assert code == 1, out
    assert out[-1].startswith("FRESHNESS: FAIL ")
    assert any(needle in line for line in out[:-1]), "\n".join(out)


def test_render_reports_unchanged_and_refuses_unresolvable_claims(
    repo: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert claims.main(["--root", str(repo.path), "render", "README.md"]) == 0
    assert capsys.readouterr().out == "unchanged README.md (8 claims)\n"
    before = repo.read("README.md")
    edit(repo, ART, '"estimates":', '"rows":')
    assert claims.main(["--root", str(repo.path), "render"]) == 1
    assert "no key 'estimates'" in capsys.readouterr().out
    assert repo.read("README.md") == before


def test_show_lists_current_values(repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert claims.main(["--root", str(repo.path), "show"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "e2.rule.recall.ci\t[0.301, 0.529]\tartifacts/e2/results.json" in lines
    assert "e2.prereg.min_prevalence\t0.0001\tprereg/0001-e2-main.md" in lines
