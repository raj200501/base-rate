"""Tests for tools/prereg.py (``make prereg-verify``): each rule can fail.

Each case builds a small git repository under tmp_path, breaks one property
of the preregistration contract (docs/PLAN.md), and checks that ``verify``
fails for that reason; the control cases show it passes when nothing is broken.
"""

from __future__ import annotations

import importlib.util
import json
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
prereg = h.load_tool("prereg")
chainlib = h.load_tool("chainlib")
ship_check = h.load_tool("ship_check")

CONFIG: dict[str, Any] = {
    "prevalences": [0.01, 0.001, 0.0001],
    "budgets_per_10k": [5, 20, 100],
    "nominal": 0.95,
}
P1 = "prereg/0001-e2-main.md"
P2 = "prereg/0002-e2-amendment.md"
ART = "artifacts/e2/results.json"


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    h.isolate_git(monkeypatch)


def linked(path: str = P1, config: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    """An artifact that cites ``path`` with the right config hash."""
    digest = chainlib.sha256_obj(CONFIG if config is None else config)
    return {"prereg": path, "prereg_config_sha256": digest, "confirmatory": True, **extra}


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Commit 1 freezes one preregistration; commit 2 banks an artifact citing it."""
    repo = h.Repo.init(tmp_path_factory.mktemp("prereg") / "base")
    repo.write("prereg/README.md", "# Preregistrations\n")
    repo.write(P1, h.prereg_text(CONFIG))
    repo.commit("freeze the E2 plan")
    repo.write_json(ART, linked())
    repo.commit("bank E2 results")
    return repo


@pytest.fixture
def repo(base: Any, tmp_path: Path) -> Any:
    return base.copy_to(tmp_path / "repo")


def problems(repo: Any) -> str:
    """All verify problems, one per line."""
    found, _, _ = prereg.verify(repo.path)
    return "\n".join(found)


def assert_fails(repo: Any, needle: str) -> None:
    text = problems(repo)
    assert needle in text, text


def fresh(tmp_path: Path, text: str) -> Any:
    """A repository whose only preregistration has the given content."""
    repo = h.Repo.init(tmp_path / "fresh")
    repo.write("prereg/README.md", "# Preregistrations\n")
    repo.write(P1, text)
    repo.commit("freeze")
    return repo


# ------------------------------------------------------------------ controls


def test_control_passes_with_exact_last_line(repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    assert prereg.main(["verify", "--root", str(repo.path)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["PREREG-VERIFY: PASS 1 preregistrations, 1 linked artifacts"]


def test_cli_runs_stdlib_only_from_the_repo_root(repo: Any) -> None:
    script = h.TOOLS / "prereg.py"
    ok = subprocess.run(
        [sys.executable, "-I", str(script), "verify"],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert (
        ok.stdout.splitlines()[-1] == "PREREG-VERIFY: PASS 1 preregistrations, 1 linked artifacts"
    )
    repo.write(P1, repo.read(P1) + "\nedited\n")
    bad = subprocess.run(
        [sys.executable, "-I", str(script), "--root", str(repo.path), "verify"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 1
    lines = bad.stdout.splitlines()
    assert lines[-1] == "PREREG-VERIFY: FAIL 1"
    assert lines[0].startswith("- prereg/0001-e2-main.md: modified in the working tree")


def test_readme_in_prereg_may_change(repo: Any) -> None:
    repo.write("prereg/README.md", "# Preregistrations\n\nMore guidance.\n")
    repo.commit("expand the README")
    assert problems(repo) == ""


def test_exploratory_artifact_without_prereg_is_not_linked(repo: Any) -> None:
    repo.write_json("artifacts/pilot/summary.json", {"exploratory": True, "value": 3})
    repo.commit("bank a pilot")
    found, n, m = prereg.verify(repo.path)
    assert (found, n, m) == ([], 1, 1)


def test_artifact_updated_after_its_prereg_still_passes(repo: Any) -> None:
    repo.write_json(ART, linked(note="rerun with more seeds"))
    repo.commit("update E2 results")
    assert problems(repo) == ""


def test_prereg_merged_from_a_side_branch_passes(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "merge")
    repo.write("prereg/README.md", "# Preregistrations\n")
    repo.commit("start")
    repo.git("checkout", "-q", "-b", "plan")
    repo.write(P1, h.prereg_text(CONFIG))
    repo.commit("freeze on a branch")
    repo.git("checkout", "-q", "main")
    repo.write("notes.txt", "unrelated\n")
    repo.commit("unrelated work")
    repo.git("merge", "-q", "--no-ff", "plan", "-m", "merge the plan")
    repo.write_json(ART, linked())
    repo.commit("bank results")
    assert problems(repo) == ""


def test_valid_amendment_passes(repo: Any) -> None:
    amended = {**CONFIG, "nominal": 0.9}
    repo.write(P2, h.prereg_text(amended, pid="0002", extra=f"amends: {P1}\n"))
    repo.commit("amend the E2 plan")
    repo.write_json("artifacts/e2/amended.json", linked(P2, amended))
    repo.commit("bank amended results")
    found, n, m = prereg.verify(repo.path)
    assert (found, n, m) == ([], 2, 2)


# ------------------------------------------------------- file format rules


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        (h.prereg_text(CONFIG, title=""), "front matter lacks 'title'"),
        (h.prereg_text(CONFIG, experiment=""), "front matter lacks 'experiment'"),
        (h.prereg_text(CONFIG).replace("id: 0001\n", ""), "front matter lacks 'id'"),
        (h.prereg_text(CONFIG, date="23 October 2026"), "is not an ISO date"),
        (h.prereg_text(CONFIG, date="2026-02-30"), "is not an ISO date"),
        (h.prereg_text(CONFIG, extra="date: 2026-10-24\n"), "key 'date' appears twice"),
        ("# No front matter\n\n```json prereg-config\n{}\n```\n", "no front matter"),
        ("---\nid: 0001\n\nnever closed\n", "front matter is not closed"),
        (h.prereg_text(CONFIG).split("```json")[0], "found 0"),
        (h.prereg_text(CONFIG) + "\n```json prereg-config\n{}\n```\n", "found 2"),
        (h.prereg_text(CONFIG).replace('"nominal": 0.95', '"nominal": 0.95,'), "not valid JSON"),
        (h.prereg_text(CONFIG).replace('"nominal": 0.95', '"nominal": NaN'), "not valid JSON"),
        (
            h.prereg_text(CONFIG).replace('"nominal": 0.95', '"nominal": 0.95, "nominal": 0.5'),
            "duplicate key 'nominal'",
        ),
        (h.prereg_text(CONFIG).replace("{", "[{", 1).replace("}\n```", "}]\n```"), "JSON object"),
    ],
    ids=[
        "no-title",
        "no-experiment",
        "no-id",
        "date-not-iso",
        "date-impossible",
        "duplicate-key",
        "no-front-matter",
        "front-matter-unclosed",
        "no-config-block",
        "two-config-blocks",
        "config-not-json",
        "config-nan",
        "config-duplicate-key",
        "config-not-object",
    ],
)
def test_format_rules(tmp_path: Path, text: str, needle: str) -> None:
    assert_fails(fresh(tmp_path, text), needle)


def test_front_matter_quotes_and_nested_lines() -> None:
    front, found = prereg.parse_front_matter(
        "---\nid: '0001'\ntitle: \"E2: main\"\nhypotheses:\n  - H1\n# comment\n---\nbody\n"
    )
    assert found == []
    assert front == {"id": "0001", "title": "E2: main", "hypotheses": ""}


def test_no_preregistrations_fails(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "empty")
    repo.write("prereg/README.md", "# Preregistrations\n")
    repo.commit("start")
    assert_fails(repo, "no preregistrations under prereg/")


def test_not_a_git_repository_fails(tmp_path: Path) -> None:
    (tmp_path / "prereg").mkdir()
    assert_fails(h.Repo(tmp_path), "git history unavailable")


# ------------------------------------------------------------ frozen rules


def _edit_prose(repo: Any) -> None:
    repo.write(P1, repo.read(P1).replace("Plan prose.", "Plan prose, revised."))


FROZEN_CASES: list[tuple[str, Callable[[Any], None], str]] = [
    (
        "modified-after-freezing",
        lambda r: (_edit_prose(r), r.commit("edit plan")),
        "changed after it was frozen; touched by 2 commits",
    ),
    (
        "deleted",
        lambda r: (r.git("rm", "-q", P1), r.commit("drop plan")),
        f"{P1}: deleted or renamed after it was frozen",
    ),
    (
        "renamed-and-edited",
        lambda r: (
            r.git("mv", P1, "prereg/0001-e2-renamed.md"),
            r.write(
                "prereg/0001-e2-renamed.md",
                r.read("prereg/0001-e2-renamed.md").replace("0.95", "0.9"),
            ),
            r.commit("rename and loosen"),
        ),
        f"{P1}: deleted or renamed after it was frozen",
    ),
    ("modified-in-working-tree", _edit_prose, "modified in the working tree since it was frozen"),
    (
        "staged-edit",
        lambda r: (_edit_prose(r), r.git("add", P1)),
        "staged changes to a frozen preregistration",
    ),
    (
        "untracked-new-prereg",
        lambda r: r.write(P2, h.prereg_text(CONFIG, pid="0002", title="")),
        f"{P2}: not committed",
    ),
    (
        "untracked-new-prereg-format-also-reported",
        lambda r: r.write(P2, h.prereg_text(CONFIG, pid="0002", title="")),
        f"{P2}: front matter lacks 'title'",
    ),
    (
        "unexpected-file",
        lambda r: (r.write("prereg/notes.txt", "draft\n"), r.commit("notes")),
        "prereg/notes.txt: unexpected file",
    ),
    (
        "number-reused",
        lambda r: (
            r.write("prereg/0001-other.md", h.prereg_text(CONFIG, pid="0009")),
            r.commit("second 0001"),
        ),
        "preregistration number 0001 is used by",
    ),
    (
        "id-reused",
        lambda r: (r.write(P2, h.prereg_text(CONFIG, pid="0001")), r.commit("same id")),
        "preregistration id '0001' is used by",
    ),
]


@pytest.mark.parametrize(
    ("mutate", "needle"), [c[1:] for c in FROZEN_CASES], ids=[c[0] for c in FROZEN_CASES]
)
def test_frozen_rules(repo: Any, mutate: Callable[[Any], None], needle: str) -> None:
    mutate(repo)
    assert_fails(repo, needle)


def test_modification_on_a_discarded_side_branch_still_counts(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "side")
    repo.write("prereg/README.md", "# Preregistrations\n")
    repo.write(P1, h.prereg_text(CONFIG))
    repo.commit("freeze")
    repo.git("checkout", "-q", "-b", "tweak")
    _edit_prose(repo)
    repo.commit("tweak the plan")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "-s", "ours", "tweak", "-m", "merge, keeping main's plan")
    assert_fails(repo, "touched by 2 commits")


def test_edit_hidden_in_a_merge_commit_fails(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "evil")
    repo.write("prereg/README.md", "# Preregistrations\n")
    repo.write(P1, h.prereg_text(CONFIG))
    repo.commit("freeze")
    repo.git("checkout", "-q", "-b", "side")
    repo.write("side.txt", "side\n")
    repo.commit("side work")
    repo.git("checkout", "-q", "main")
    repo.write("main.txt", "main\n")
    repo.commit("main work")
    repo.git("merge", "-q", "--no-ff", "--no-commit", "side")
    _edit_prose(repo)
    repo.git("add", P1)
    repo.git("commit", "-q", "-m", "merge side")
    assert_fails(repo, "content at HEAD differs from its freezing commit")


# --------------------------------------------------------------- amendments


def test_amendment_citing_a_missing_prereg_fails(repo: Any) -> None:
    repo.write(P2, h.prereg_text(CONFIG, pid="0002", extra="amends: prereg/0007-nothing.md\n"))
    repo.commit("amend nothing")
    assert_fails(repo, "amends 'prereg/0007-nothing.md', which is not another preregistration")


def test_amendment_citing_itself_fails(repo: Any) -> None:
    repo.write(P2, h.prereg_text(CONFIG, pid="0002", extra=f"amends: {P2}\n"))
    repo.commit("amend itself")
    assert_fails(repo, "which is not another preregistration")


def test_amendment_committed_with_the_original_fails(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "same")
    repo.write(P1, h.prereg_text(CONFIG))
    repo.write(P2, h.prereg_text(CONFIG, pid="0002", extra=f"amends: {P1}\n"))
    repo.commit("freeze both at once")
    assert_fails(repo, f"amends {P1}, which was not committed strictly earlier")


# ----------------------------------------------------------- artifact links


ARTIFACT_CASES: list[tuple[str, Callable[[Any], None], str]] = [
    (
        "config-hash-mismatch",
        lambda r: (r.write_json(ART, {**linked(), "prereg_config_sha256": "0" * 64}), r.commit()),
        f"does not match {P1}",
    ),
    (
        "hash-of-a-different-config",
        lambda r: (r.write_json(ART, linked(config={**CONFIG, "nominal": 0.9})), r.commit()),
        f"does not match {P1}",
    ),
    (
        "cites-missing-prereg",
        lambda r: (r.write_json(ART, linked("prereg/0005-missing.md")), r.commit()),
        "cites 'prereg/0005-missing.md', which is not a tracked prereg",
    ),
    (
        "cites-non-canonical-path",
        lambda r: (r.write_json(ART, linked("./" + P1)), r.commit()),
        "which is not a tracked prereg",
    ),
    (
        "prereg-null",
        lambda r: (r.write_json(ART, {**linked(), "prereg": None}), r.commit()),
        "cites None",
    ),
    (
        "confirmatory-without-prereg",
        lambda r: (r.write_json(ART, {"confirmatory": True, "estimates": []}), r.commit()),
        "confirmatory but cites no preregistration",
    ),
    (
        "invalid-json",
        lambda r: (r.write(ART, '{"prereg": '), r.commit()),
        f"{ART}: not valid JSON",
    ),
    (
        "staged-not-committed",
        lambda r: (
            r.write_json("artifacts/e2/new.json", linked()),
            r.git("add", "artifacts/e2/new.json"),
        ),
        "artifacts/e2/new.json: not committed",
    ),
]


@pytest.mark.parametrize(
    ("mutate", "needle"), [c[1:] for c in ARTIFACT_CASES], ids=[c[0] for c in ARTIFACT_CASES]
)
def test_artifact_rules(repo: Any, mutate: Callable[[Any], None], needle: str) -> None:
    mutate(repo)
    assert_fails(repo, needle)


def test_artifact_in_the_same_commit_as_its_prereg_fails(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "same")
    repo.write(P1, h.prereg_text(CONFIG))
    repo.write_json(ART, linked())
    repo.commit("freeze and bank together")
    assert_fails(repo, f"not strictly after {P1} was frozen")


def test_artifact_committed_before_its_prereg_fails(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "early")
    repo.write_json(ART, linked())
    repo.commit("bank first")
    repo.write(P1, h.prereg_text(CONFIG))
    repo.commit("freeze afterwards")
    assert_fails(repo, f"not strictly after {P1} was frozen")


def test_artifact_renamed_from_a_file_older_than_the_prereg_fails(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "renamed")
    rows = [{"monitor": f"m{i}", "estimate": i / 100} for i in range(40)]
    repo.write_json("artifacts/e2/draft.json", {"estimates": rows})
    repo.commit("bank a draft before the plan exists")
    repo.write(P1, h.prereg_text(CONFIG))
    repo.commit("freeze")
    repo.git("mv", "artifacts/e2/draft.json", ART)
    repo.write_json(ART, {**linked(), "estimates": rows})
    repo.commit("relabel the draft as confirmatory")
    assert_fails(repo, f"{ART}: committed in")


# ------------------------------------------------------------- hash command


def test_hash_command_matches_ship_check_and_the_fixture(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = h.CLAIMS_REPO / P1
    assert prereg.main(["hash", str(fixture)]) == 0
    printed = capsys.readouterr().out.strip()
    recorded = json.loads((h.CLAIMS_REPO / ART).read_text(encoding="utf-8"))
    assert printed == recorded["prereg_config_sha256"]
    assert printed == ship_check.canonical_sha256(CONFIG)


def test_hash_accepts_root_after_the_subcommand(
    repo: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert prereg.main(["hash", P1, "--root", str(repo.path)]) == 0
    assert capsys.readouterr().out.strip() == chainlib.sha256_obj(CONFIG)


def test_hash_refuses_an_invalid_file(repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    repo.write(P2, h.prereg_text(CONFIG, date="soon"))
    assert prereg.main(["--root", str(repo.path), "hash", P2]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "is not an ISO date" in captured.err
    assert prereg.main(["--root", str(repo.path), "hash", "prereg/0099-none.md"]) == 2


def test_config_parsing_agrees_with_ship_check(repo: Any, tmp_path: Path) -> None:
    ctx = ship_check.Ctx(orig=repo.path, clone=repo.path, tmp=tmp_path, fast=True, env={})
    _, config = ship_check.prereg_config(ctx, P1)
    assert ship_check.canonical_sha256(config) == prereg.config_sha256(repo.read(P1))


# ------------------------------------------------------- draft stage (D19)

DRAFT: dict[str, Any] = {**CONFIG, "method": "TO_BE_SET_FROM_E0"}
FINAL: dict[str, Any] = {**CONFIG, "method": "composite"}


def history(
    tmp_path: Path, configs: list[dict[str, Any]], *, artifact_at: int | None = None
) -> Any:
    """One commit of P1 per config (its prose changes each time), then the artifact.

    With ``artifact_at = i`` the artifact, which cites the last config, is
    committed right after version ``i`` of P1 instead of at the end.
    """
    repo = h.Repo.init(tmp_path / "drafted")
    repo.write("prereg/README.md", "# Preregistrations\n")
    for i, config in enumerate(configs):
        repo.write(P1, h.prereg_text(config, body=f"Plan prose, version {i}.\n"))
        repo.commit(f"plan version {i}")
        if artifact_at == i:
            repo.write_json(ART, linked(config=configs[-1]))
            repo.commit("bank")
    if artifact_at is None:
        repo.write_json(ART, linked(config=configs[-1]))
        repo.commit("bank")
    return repo


def plan_commits(repo: Any) -> list[str]:
    return repo.git("log", "--reverse", "--format=%H", "--", P1).splitlines()


def test_draft_then_freeze_passes_and_is_noted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = history(tmp_path, [DRAFT, FINAL])
    draft, freeze = plan_commits(repo)
    notes: list[str] = []
    found, n, m = prereg.verify(repo.path, notes)
    assert (found, n, m) == ([], 1, 1)
    assert notes == [
        f"{P1}: frozen in {freeze[:12]} after draft commit(s) {draft[:12]} "
        "that held a TO_BE_SET placeholder"
    ]
    assert prereg.main(["verify", "--root", str(repo.path)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        f"note: {notes[0]}",
        "PREREG-VERIFY: PASS 1 preregistrations, 1 linked artifacts",
    ]


def test_two_drafts_then_freeze_passes(tmp_path: Path) -> None:
    first = {**CONFIG, "method": "TO_BE_SET", "seed": "TO_BE_SET_LATER"}
    second = {**CONFIG, "method": "composite", "seed": "TO_BE_SET_LATER"}
    repo = history(tmp_path, [first, second, {**CONFIG, "method": "composite", "seed": 7}])
    notes: list[str] = []
    assert prereg.verify(repo.path, notes)[0] == []
    one, two, _ = plan_commits(repo)
    assert f"draft commit(s) {one[:12]}, {two[:12]} that held" in notes[0]


DRAFT_CASES: list[tuple[str, list[dict[str, Any]], int | None, str]] = [
    (
        "edited-after-freezing",
        [DRAFT, FINAL, FINAL],
        None,
        "changed after it was frozen; touched by 3 commits",
    ),
    (
        "placeholder-put-back-after-freezing",
        [DRAFT, FINAL, DRAFT],
        None,
        "changed after it was frozen; touched by 3 commits",
    ),
    (
        "still-a-draft-at-head",
        [DRAFT],
        None,
        "still a draft: its config holds a TO_BE_SET placeholder at /method",
    ),
    (
        "freeze-changes-a-value-the-draft-fixed",
        [DRAFT, {**FINAL, "nominal": 0.9}],
        None,
        "changes a config value that draft",
    ),
    (
        "freeze-adds-a-key",
        [DRAFT, {**FINAL, "extra": 1}],
        None,
        "changes a config value that draft",
    ),
    (
        "freeze-drops-a-key",
        [DRAFT, {k: v for k, v in FINAL.items() if k != "nominal"}],
        None,
        "changes a config value that draft",
    ),
    (
        "second-draft-changes-a-value-the-first-fixed",
        [DRAFT, {**DRAFT, "nominal": 0.9}, {**FINAL, "nominal": 0.9}],
        None,
        "changes a config value that draft",
    ),
    (
        "lookalike-value-is-not-a-placeholder",
        [{**CONFIG, "method": "TO_BE_SETTLED"}, FINAL],
        None,
        "changed after it was frozen; touched by 2 commits",
    ),
    (
        "lower-case-value-is-not-a-placeholder",
        [{**CONFIG, "method": "to_be_set"}, FINAL],
        None,
        "changed after it was frozen; touched by 2 commits",
    ),
    (
        "artifact-between-draft-and-freeze",
        [DRAFT, FINAL],
        0,
        f"not strictly after {P1} was frozen in",
    ),
]


@pytest.mark.parametrize(
    ("configs", "artifact_at", "needle"),
    [c[1:] for c in DRAFT_CASES],
    ids=[c[0] for c in DRAFT_CASES],
)
def test_draft_rules(
    tmp_path: Path, configs: list[dict[str, Any]], artifact_at: int | None, needle: str
) -> None:
    assert_fails(history(tmp_path, configs, artifact_at=artifact_at), needle)


def test_artifact_in_the_freezing_commit_fails(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "same")
    repo.write(P1, h.prereg_text(DRAFT))
    repo.commit("draft")
    repo.write(P1, h.prereg_text(FINAL))
    repo.write_json(ART, linked(config=FINAL))
    repo.commit("freeze and bank together")
    assert_fails(repo, f"not strictly after {P1} was frozen")


def test_placeholder_in_prose_only_is_not_a_draft(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "prose")
    repo.write(P1, h.prereg_text(CONFIG, body="Method: TO_BE_SET.\n"))
    repo.commit("plan")
    repo.write(P1, h.prereg_text(CONFIG, body="Method: composite.\n"))
    repo.commit("fill the prose")
    assert_fails(repo, "changed after it was frozen; touched by 2 commits")


def test_commit_with_an_unparsable_config_is_not_a_draft(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "broken")
    repo.write(P1, h.prereg_text(DRAFT).replace('"method"', '"method" "'))
    repo.commit("broken plan")
    repo.write(P1, h.prereg_text(FINAL))
    repo.commit("repair")
    assert_fails(repo, "changed after it was frozen; touched by 2 commits")


def test_amendment_of_a_draft_must_follow_its_freeze(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "amend")
    repo.write(P1, h.prereg_text(DRAFT))
    repo.commit("draft")
    repo.write(P2, h.prereg_text(CONFIG, pid="0002", extra=f"amends: {P1}\n"))
    repo.commit("amend the draft")
    repo.write(P1, h.prereg_text(FINAL))
    repo.commit("freeze")
    assert_fails(repo, f"{P2}: amends {P1}, which was not committed strictly earlier")


def test_placeholder_paths_finds_nested_values_only_of_the_exact_form() -> None:
    config = {
        "a": ["x", "TO_BE_SET"],
        "b": {"c": "TO_BE_SET_FROM_E0", "d": "TO_BE_SETTLED"},
        "e": "to_be_set",
        "f": "TO_BE_SET_",
        "g": 3,
    }
    assert prereg.placeholder_paths(config) == ["/a/1", "/b/c"]


FILL_CASES: list[tuple[Any, Any, bool]] = [
    ({"a": "TO_BE_SET", "b": 1}, {"a": {"x": [1, 2]}, "b": 1}, True),
    ({"a": [1, "TO_BE_SET"]}, {"a": [1, "composite"]}, True),
    ({"a": 1}, {"a": 1.0}, False),
    ({"a": 1}, {"a": True}, False),
    ({"a": 0}, {"a": False}, False),
    ({"a": [1, 2]}, {"a": [1]}, False),
    ({"a": 1}, {"a": 1, "b": 2}, False),
    ({"a": "TO_BE_SET", "b": 1}, {"a": 5}, False),
    ({"a": {"b": 1}}, {"a": [1]}, False),
    ({"a": None}, {"a": None}, True),
]


@pytest.mark.parametrize(("draft", "final", "expected"), FILL_CASES)
def test_fills_only_placeholders_agrees_with_ship_check(
    draft: Any, final: Any, expected: bool
) -> None:
    assert prereg.fills_only_placeholders(draft, final) is expected
    assert ship_check.keeps_fixed_values(draft, final) is expected
    assert bool(prereg.placeholder_paths(draft)) == ship_check.has_placeholder(draft)


# ------------------------------------------- merges, renames, CRLF (review D21)


def _branch_merge(repo: Any, edit: Callable[[], None], message: str) -> str:
    """A side commit and a main commit, then a merge whose commit also runs ``edit``."""
    repo.git("checkout", "-q", "-b", f"side-{message[:8].replace(' ', '-')}")
    repo.write(f"side-{message[:8]}.txt", "side\n")
    repo.commit("side work")
    side = repo.git("rev-parse", "--abbrev-ref", "HEAD")
    repo.git("checkout", "-q", "main")
    repo.write(f"main-{message[:8]}.txt", "main\n")
    repo.commit("main work")
    repo.git("merge", "-q", "--no-ff", "--no-commit", side)
    edit()
    repo.git("add", "-A")
    repo.git("commit", "-q", "--no-verify", "-m", message)
    return repo.head()


def test_freeze_in_a_merge_then_an_edit_fails(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "evil")
    repo.write(P1, h.prereg_text(DRAFT, body="v0\n"))
    repo.commit("draft")
    _branch_merge(
        repo,
        lambda: repo.write(P1, h.prereg_text({**FINAL, "method": "bootstrap"}, body="vm\n")),
        "merge fills the method",
    )
    repo.write(P1, h.prereg_text(FINAL, body="v2\n"))
    repo.commit("edit after the merge froze it")
    repo.write_json(ART, linked(config=FINAL))
    repo.commit("bank")
    text = problems(repo)
    assert "frozen in merge commit" in text and "changed after it was frozen" in text, text


def test_a_transient_change_through_merges_after_the_freeze_fails(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "transient")
    repo.write(P1, h.prereg_text(CONFIG))
    repo.commit("freeze")
    frozen = repo.read(P1)
    _branch_merge(
        repo,
        lambda: repo.write(P1, frozen.replace('"nominal": 0.95', '"nominal": 0.5')),
        "merge loosens the plan",
    )
    repo.write_json(ART, linked())
    repo.commit("bank under the loosened plan")
    _branch_merge(repo, lambda: repo.write(P1, frozen), "merge restores the plan")
    assert_fails(repo, "changed after it was frozen")


def test_artifact_committed_inside_a_merge_before_the_freeze_fails(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "mergeart")
    repo.write(P1, h.prereg_text(DRAFT))
    repo.commit("draft")
    _branch_merge(repo, lambda: repo.write_json(ART, linked(config=FINAL)), "merge banks early")
    repo.write(P1, h.prereg_text(FINAL))
    repo.commit("freeze")
    repo.git("rm", "-q", ART)
    repo.commit("drop the early artifact")
    repo.write_json(ART, linked(config=FINAL))
    repo.commit("bank again after the freeze")
    assert_fails(repo, f"not strictly after {P1} was frozen")


def test_artifact_renamed_after_the_freeze_from_an_early_file_fails(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "renamed")
    repo.write(P1, h.prereg_text(DRAFT))
    repo.commit("draft")
    repo.write_json("artifacts/e2/tmp.json", linked(config=FINAL))
    repo.commit("bank under another name")
    repo.write(P1, h.prereg_text(FINAL))
    repo.commit("freeze")
    repo.git("mv", "artifacts/e2/tmp.json", ART)
    repo.commit("rename after the freeze")
    assert_fails(repo, f"not strictly after {P1} was frozen")


def test_a_similar_exploratory_file_is_not_followed_as_a_copy(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "copy")
    repo.write(P1, h.prereg_text(DRAFT))
    repo.commit("draft")
    rows = [{"monitor": f"m{i}", "estimate": i / 10} for i in range(8)]
    repo.write_json("artifacts/e2/pilot.json", {"exploratory": True, "rows": rows})
    repo.commit("exploratory pilot, no prereg field")
    repo.write(P1, h.prereg_text(FINAL))
    repo.commit("freeze")
    repo.write_json(ART, linked(config=FINAL, rows=rows))
    repo.commit("bank")
    assert problems(repo) == ""


def test_crlf_draft_at_head_fails(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "crlf")
    repo.write(P1, h.prereg_text(DRAFT).replace("\n", "\r\n"))
    repo.commit("draft with CRLF line endings")
    repo.write_json(ART, linked(config=DRAFT))
    repo.commit("bank")
    assert_fails(repo, "still a draft")


def test_placeholder_with_a_trailing_newline_is_not_a_draft(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "newline")
    repo.write(P1, h.prereg_text({**CONFIG, "method": "TO_BE_SET\n"}))
    repo.commit("plan")
    repo.write(P1, h.prereg_text(FINAL))
    repo.commit("edit")
    assert_fails(repo, "changed after it was frozen; touched by 2 commits")


def test_amendment_drafted_before_its_original_froze_fails(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "amenddraft")
    repo.write(P1, h.prereg_text(DRAFT))
    repo.commit("P1 draft")
    amend = {"change": "TO_BE_SET", "x": 1}
    repo.write(P2, h.prereg_text(amend, pid="0002", extra=f"amends: {P1}\n"))
    repo.commit("P2 draft")
    repo.write(P1, h.prereg_text(FINAL))
    repo.commit("P1 frozen")
    repo.write(P2, h.prereg_text({**amend, "change": "more"}, pid="0002", extra=f"amends: {P1}\n"))
    repo.commit("P2 frozen")
    assert_fails(repo, f"{P2}: amends {P1}, which was not committed strictly earlier")


def test_amendment_drafted_before_its_original_existed_fails(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "amendfirst")
    amend = {"change": "TO_BE_SET", "x": 1}
    repo.write(P2, h.prereg_text(amend, pid="0002", extra=f"amends: {P1}\n"))
    repo.commit("P2 draft first")
    repo.write(P1, h.prereg_text(CONFIG))
    repo.commit("P1 frozen")
    repo.write(P2, h.prereg_text({**amend, "change": "more"}, pid="0002", extra=f"amends: {P1}\n"))
    repo.commit("P2 frozen")
    assert_fails(repo, f"{P2}: amends {P1}, which was not committed strictly earlier")


def test_draft_on_a_side_branch_that_the_freeze_does_not_descend_from_fails(
    tmp_path: Path,
) -> None:
    repo = h.Repo.init(tmp_path / "sidedraft")
    repo.write("README.md", "x\n")
    repo.commit("start")
    repo.git("checkout", "-q", "-b", "side")
    repo.write(P1, h.prereg_text(DRAFT, body="side draft\n"))
    repo.commit("draft on a side branch")
    repo.git("checkout", "-q", "main")
    repo.write(P1, h.prereg_text(FINAL))
    repo.commit("freeze on main without the draft")
    repo.git("merge", "-q", "-s", "ours", "side", "-m", "merge, keeping main's plan")
    # topological order decides whether the side draft counts as an earlier draft
    # that the freeze does not descend from, or as a version after the freeze
    text = problems(repo)
    assert "does not precede the freezing commit" in text or "changed after it was frozen" in text


def test_symlinked_preregistration_fails(tmp_path: Path) -> None:
    repo = h.Repo.init(tmp_path / "symlink")
    repo.write("docs/plan.md", h.prereg_text(CONFIG))
    (repo.path / "prereg").mkdir()
    (repo.path / P1).symlink_to("../docs/plan.md")
    repo.commit("plan behind a symlink")
    assert_fails(repo, "not a regular file")


# ------------------------------------------- amendment citations on artifacts


AMEND_CONFIG: dict[str, Any] = {"change": "relabel by replay"}


def _amended(tmp_path: Path, amendment_first: bool) -> Any:
    repo = h.Repo.init(tmp_path / "amended")
    repo.write(P1, h.prereg_text(CONFIG))
    repo.commit("freeze the plan")
    amendment = h.prereg_text(AMEND_CONFIG, pid="0002", extra=f"amends: {P1}\n")
    art = {
        **linked(),
        "prereg_amendments": [
            {"prereg": P2, "prereg_config_sha256": chainlib.sha256_obj(AMEND_CONFIG)}
        ],
    }
    if amendment_first:
        repo.write(P2, amendment)
        repo.commit("freeze the amendment")
        repo.write_json(ART, art)
        repo.commit("bank under the amended plan")
    else:
        repo.write_json(ART, art)
        repo.commit("bank before the amendment")
        repo.write(P2, amendment)
        repo.commit("amend afterwards")
    return repo


def test_artifact_citing_an_earlier_amendment_passes(tmp_path: Path) -> None:
    assert problems(_amended(tmp_path, amendment_first=True)) == ""


def test_artifact_banked_before_an_amendment_it_cites_fails(tmp_path: Path) -> None:
    assert_fails(_amended(tmp_path, amendment_first=False), f"not strictly after {P2} was frozen")


def test_amendment_citation_with_a_wrong_hash_fails(tmp_path: Path) -> None:
    repo = _amended(tmp_path, amendment_first=True)
    data = repo.read_json(ART)
    data["prereg_amendments"][0]["prereg_config_sha256"] = "0" * 64
    repo.write_json(ART, data)
    repo.commit("wrong hash")
    assert_fails(repo, f"does not match {P2}")
