"""Tests for tools/claimcheck.py and the claim mechanism in tools/claims.py.

The integration cases render the fixture under tests/fixtures/integrity/
claims_repo into a throwaway git repository, break one property, and check
that claimcheck fails for that reason (in the spirit of raise-v1's
tests/mutation_test.py: a gate that cannot fail is decoration). The unit cases
pin the number scanner, the selector grammar, formats, and the matcher.
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

DOCS = ("README.md", "docs/REPORT.md")
README_T = "docs/templates/README.md.tmpl"
REPORT_T = "docs/templates/REPORT.md.tmpl"
REGISTRY = "docs/claims.json"
ART = "artifacts/e2/results.json"


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    h.isolate_git(monkeypatch)


def build_claims_repo(path: Path) -> Any:
    """The fixture repository, committed, rendered, and committed again."""
    repo = h.Repo.init(path)
    repo.copy_in(h.CLAIMS_REPO, ".")
    repo.copy_in(h.REPO_ROOT / "docs" / "BUILD_BRIEF.md", "docs/BUILD_BRIEF.md")
    repo.copy_in(h.REPO_ROOT / "docs" / "BUILD_BRIEF.sha256", "docs/BUILD_BRIEF.sha256")
    repo.commit("bank fixture artifacts and templates")
    assert claims.main(["--root", str(repo.path), "render"]) == 0
    repo.commit("render documents")
    return repo


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return build_claims_repo(tmp_path_factory.mktemp("claims") / "base")


@pytest.fixture
def repo(base: Any, tmp_path: Path) -> Any:
    return base.copy_to(tmp_path / "repo")


def run(repo: Any, capsys: pytest.CaptureFixture[str], *docs: str) -> tuple[int, list[str]]:
    capsys.readouterr()
    code = claimcheck.main(["--root", str(repo.path), *(docs or DOCS)])
    return code, capsys.readouterr().out.splitlines()


def render(repo: Any) -> None:
    assert claims.main(["--root", str(repo.path), "render"]) == 0


def edit(repo: Any, rel: str, old: str, new: str, count: int = 1) -> None:
    text = repo.read(rel)
    assert old in text, f"{old!r} not in {rel}"
    repo.write(rel, text.replace(old, new, count))


def append(repo: Any, rel: str, text: str) -> None:
    repo.write(rel, repo.read(rel) + text)


def add_claim(repo: Any, cid: str, **spec: str) -> None:
    registry = repo.read_json(REGISTRY)
    registry["claims"][cid] = spec
    repo.write_json(REGISTRY, registry)


def template_line(repo: Any, line: str) -> None:
    """Append a line to the README template, re-render, and commit."""
    append(repo, README_T, line + "\n")
    render(repo)
    repo.commit("edit template")


# ------------------------------------------------------------------ controls


def test_control_passes_with_counts(repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(repo, capsys)
    assert code == 0, out
    assert out == [
        "CLAIMCHECK: PASS 2 documents, 12 claim spans from 2 banked sources, 10 allowances"
    ]


def test_default_checks_every_registered_document(
    repo: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert claimcheck.main(["--root", str(repo.path)]) == 0
    assert capsys.readouterr().out.startswith("CLAIMCHECK: PASS 2 documents")


def test_rendered_document_has_values_not_placeholders(repo: Any) -> None:
    readme = repo.read("README.md")
    assert "{{" not in readme
    assert "recall at budget was 0.412 (95% interval [0.301, 0.529]), from 1,212" in readme
    assert "Hypotheses supported: 2 of 3. The smallest preregistered prevalence is 0.0001." in (
        readme
    )


def test_cli_from_repo_root(repo: Any) -> None:
    proc = subprocess.run(
        [sys.executable, "-I", str(h.TOOLS / "claimcheck.py"), *DOCS],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.splitlines()[-1].startswith("CLAIMCHECK: PASS 2 documents")


def test_stale_but_banked_value_passes_claimcheck(
    repo: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # A value that was banked stays banked; catching it is freshness's job.
    edit(repo, ART, '"estimate": 0.4123', '"estimate": 0.4151')
    repo.commit("re-bank E2 without re-rendering")
    code, out = run(repo, capsys)
    assert code == 0, out


# ------------------------------------------------------------------ mutations

MUTATIONS: list[tuple[str, Callable[[Any], None], str]] = [
    (
        "fabricated-number-in-template",
        lambda r: template_line(r, "We also reached 0.981 on a held-out slice."),
        "number '0.981' is outside any claim or allowance | We also reached 0.981",
    ),
    (
        "number-in-code-fence-is-still-checked",
        lambda r: template_line(r, "```\nfinal_score = 0.777\n```"),
        "number '0.777' is outside",
    ),
    (
        "number-in-html-comment-is-still-checked",
        lambda r: template_line(r, "<!-- recall was 0.9 -->"),
        "number '0.9' is outside",
    ),
    (
        "claim-adjacent-to-url-not-swallowed",
        lambda r: template_line(r, "See https://example.com/run/12345 which reports 0.633."),
        "number '0.633' is outside",
    ),
    (
        "reworded-claim-typed-as-literal",
        lambda r: (
            edit(r, README_T, "{{claim:e2.rule.recall}}", "0.412"),
            render(r),
            r.commit("inline the value"),
        ),
        "number '0.412' is outside",
    ),
    (
        "glued-units-are-numbers",
        lambda r: template_line(r, "The cascade was 3x cheaper with a 1.5B model."),
        "number '3' is outside",
    ),
    (
        "heading-without-section-dot",
        lambda r: template_line(r, "## 3 of 5 hypotheses held"),
        "number '3' is outside",
    ),
    (
        "placeholder-cannot-fake-a-list-marker",
        lambda r: template_line(r, "{{name:v2a}} 42. caught"),
        "number '42' is outside",
    ),
    (
        "hand-edited-value-in-document",
        lambda r: (edit(r, "README.md", "0.412", "0.481"), r.commit("tweak")),
        "'0.481' for claim e2.rule.recall is not a rendering of any committed version",
    ),
    (
        "hand-added-text-at-end",
        lambda r: (append(r, "README.md", "Recall later rose to 0.5.\n"), r.commit("add")),
        "text after the end of its template",
    ),
    (
        "hand-edited-literal-text",
        lambda r: (
            edit(r, "README.md", "independent source runs.", "independent source runs (n=7)."),
            r.commit("annotate"),
        ),
        "README.md:5:",
    ),
    (
        "template-edited-not-rerendered",
        lambda r: (
            edit(r, README_T, "test fixture", "test fixture with {{param:2}} parts"),
            r.commit("edit template only"),
        ),
        "does not match its template docs/templates/README.md.tmpl:3",
    ),
    (
        "unregistered-claim",
        lambda r: (
            append(r, README_T, "Extra: {{claim:e2.unknown}}.\n"),
            r.commit("use an unknown claim"),
        ),
        "claim 'e2.unknown' is not registered in docs/claims.json",
    ),
    (
        "format-with-literal-digits",
        lambda r: (
            add_claim(r, "e2.bad", source=ART, select="hypotheses|len", format="{} of 3"),
            r.commit("bad format"),
        ),
        "claim 'e2.bad': format '{} of 3' has digits outside its fields",
    ),
    (
        "invalid-claim-used",
        lambda r: (
            add_claim(r, "e2.bad", source="results.json", select="x"),
            append(r, README_T, "Bad: {{claim:e2.bad}}.\n"),
            r.commit("bad source"),
        ),
        "claim 'e2.bad' is invalid in docs/claims.json",
    ),
    (
        "source-not-tracked",
        lambda r: (
            r.write_json("artifacts/e2/new.json", {"v": 1}),
            add_claim(r, "e2.new", source="artifacts/e2/new.json", select="v"),
            append(r, README_T, "New: {{claim:e2.new}}.\n"),
            r.commit("use new claim", REGISTRY, README_T),
            render(r),
        ),
        "source artifacts/e2/new.json is not tracked by git, so it is not banked",
    ),
    (
        "source-staged-not-committed",
        lambda r: (
            r.write_json("artifacts/e2/new.json", {"v": 1}),
            add_claim(r, "e2.new", source="artifacts/e2/new.json", select="v"),
            append(r, README_T, "New: {{claim:e2.new}}.\n"),
            r.commit("use new claim", REGISTRY, README_T),
            r.git("add", "artifacts/e2/new.json"),
            render(r),
        ),
        "has never been committed, so it is not banked",
    ),
    (
        "uncommitted-new-value",
        lambda r: (edit(r, ART, '"estimate": 0.4123', '"estimate": 0.4151'), render(r)),
        "the work-tree source gives this value: commit it to bank it",
    ),
    (
        "ambiguous-selector",
        lambda r: (
            add_claim(
                r, "e2.amb", source=ART, select="estimates[metric=recall_at_budget].estimate"
            ),
            append(r, README_T, "Ambiguous: {{claim:e2.amb}}.\n"),
            r.commit("ambiguous claim"),
        ),
        "matches 2 elements, needs exactly 1",
    ),
    (
        "brief-quote-not-verbatim",
        lambda r: template_line(r, "The brief asks for {{brief:At least 9 monitor families}}."),
        "is not quoted verbatim from docs/BUILD_BRIEF.md",
    ),
    (
        "brief-tampered",
        lambda r: (
            append(r, "docs/BUILD_BRIEF.md", "\nAt least 9 monitor families.\n"),
            r.commit(),
        ),
        "docs/BUILD_BRIEF.md does not match docs/BUILD_BRIEF.sha256",
    ),
    (
        "date-impossible",
        lambda r: template_line(r, "Frozen on {{date:2026-13-01}}."),
        "{{date:2026-13-01}} is not YYYY, YYYY-MM, or YYYY-MM-DD",
    ),
    (
        "version-not-a-version",
        lambda r: template_line(r, "Python {{version:3}}."),
        "{{version:3}} is not a version",
    ),
    (
        "allowance-without-a-number",
        lambda r: template_line(r, "A {{param:large}} grid."),
        "{{param:large}} holds no number",
    ),
    (
        "allowance-too-long",
        lambda r: template_line(r, "{{param:1 and also a whole sentence that hides 0.42 inside}}"),
        "is longer than 40 characters",
    ),
    (
        "name-with-spaces",
        lambda r: template_line(r, "{{name:qwen 2.5}}"),
        "must be one token",
    ),
    (
        "unknown-placeholder-kind",
        lambda r: (append(r, README_T, "{{number:5}} monitors.\n"), r.commit()),
        "unknown placeholder kind 'number'",
    ),
    (
        "malformed-placeholder",
        lambda r: (append(r, README_T, "Broken {{claim:e2.rule.recall}.\n"), r.commit()),
        "malformed {{claim:...}}",
    ),
    (
        "registry-duplicate-key",
        lambda r: (
            r.write(
                REGISTRY, r.read(REGISTRY).replace('"claims": {', '"claims": {"x": {}, "x": {},', 1)
            ),
            r.commit(),
        ),
        "duplicate key 'x'",
    ),
    (
        "registry-unknown-claim-key",
        lambda r: (
            add_claim(r, "e2.extra", source=ART, select="hypotheses|len", value="3"),
            r.commit(),
        ),
        "unknown keys ['value']",
    ),
    (
        "document-missing",
        lambda r: (r.git("rm", "-q", "docs/REPORT.md"), r.commit()),
        "docs/REPORT.md: file not found",
    ),
    (
        "template-missing",
        lambda r: (r.git("rm", "-q", REPORT_T), r.commit()),
        "docs/REPORT.md: its template docs/templates/REPORT.md.tmpl is missing",
    ),
    (
        "registry-missing",
        lambda r: (r.git("rm", "-q", REGISTRY), r.commit()),
        "docs/claims.json not found",
    ),
    (
        "registry-missing-still-names-missing-documents",
        lambda r: (r.git("rm", "-q", REGISTRY, "docs/REPORT.md"), r.commit()),
        "docs/REPORT.md: file not found",
    ),
]


@pytest.mark.parametrize(
    ("mutate", "needle"), [m[1:] for m in MUTATIONS], ids=[m[0] for m in MUTATIONS]
)
def test_mutation_fails_claimcheck(
    repo: Any, capsys: pytest.CaptureFixture[str], mutate: Callable[[Any], None], needle: str
) -> None:
    mutate(repo)
    code, out = run(repo, capsys)
    assert code == 1, out
    assert out[-1].startswith("CLAIMCHECK: FAIL ")
    assert any(needle in line for line in out[:-1]), "\n".join(out)


def test_unregistered_document_fails(repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    repo.write("NOTES.md", "No numbers here.\n")
    repo.commit("notes")
    code, out = run(repo, capsys, "NOTES.md")
    assert code == 1
    assert "FAIL NOTES.md: no template registered for it in docs/claims.json" in out[0]


def test_hand_written_document_without_a_template_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The build brief as README.md: full of numbers, no registry, no template.
    repo = h.Repo.init(tmp_path / "brief")
    repo.copy_in(h.REPO_ROOT / "docs" / "BUILD_BRIEF.md", "README.md")
    repo.commit("brief as README")
    code, out = run(repo, capsys, "README.md", "docs/REPORT.md")
    assert code == 1
    assert out == [
        "FAIL docs/REPORT.md: file not found",
        "FAIL docs/claims.json not found: results documents are rendered from templates whose "
        "claims it registers (docs/CLAIMS.md)",
        "CLAIMCHECK: FAIL 2 problems",
    ]


# ------------------------------------------------------------- number scanner

TOP = frozenset({"artifacts", "prereg", "docs", "src"})


@pytest.mark.parametrize(
    "text",
    [
        "Gates G5, G11, RQ1, E2, H1, D5, M2, v1.0, sha256, and qwen2.5 are identifiers.",
        "1. first item\n2) second item\n  10. nested item\n> 3. quoted item",
        "## 3. Method\n### 3.1. Data\n#### 10.2. Loop",
        "See section 6, sections 6 to, step 11, Table 2, Figure 3, Fig. 4, appendix 1, rung 7.",
        "Non-negotiable 1 and milestone 2 and §6 and § 10.1.",
        "Paths `prereg/0001-e0.md`, artifacts/e2/results.json, ./docs/fig1.svg.",
        "Links [results](artifacts/x2.json) and [paper](https://arxiv.org/abs/2512.22154).",
        "Bare https://example.com/run/12345 and www.example.org/2026/page.",
        "Commit 9330c9d396b328cef4736356e0c8962218f0ecd2 and short 9330c9d.",
        'A chart <svg viewBox="0 0 720 320"><circle cx="137.8" cy="229.9" r="4.5"/></svg>.',
        '<img src="figs/a1.svg" width="640" height="480"> and <a href="x/3">link</a>.',
        "A footnote[^1] and [^note2].\n\n[^1]: text",
    ],
)
def test_scanner_exclusions(text: str) -> None:
    assert claims.find_numbers(text, TOP) == []


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("Recall was 0.42 at 1e-3.", ["0.42", "1e-3"]),
        ("10,000 traces and 2,000 replicates", ["10,000", "2,000"]),
        ("95% interval [0.31, 0.55]", ["95", "0.31", "0.55"]),
        ("3x cheaper, a 1.5B model, 500ms", ["3", "1.5", "500"]),
        ("about .5 of them", [".5"]),
        ("3/n bound", ["3"]),
        ("## 3 of 5 held", ["3", "5"]),
        ("### 3.1 Data", ["3.1"]),
        ("| rule | 7 |", ["7"]),
        ("2026-10-23", ["2026", "10", "23"]),
        ("COVID-19 data", ["19"]),
        ("runs/123/log is not a tracked directory", ["123"]),
        ("[2] and [12]: reference labels are not exempt", ["2", "12"]),
        ("a word.5 is not, x 5.", ["5"]),
        ('<text x="1" y="2">0.981</text>', ["0.981"]),
        ("deadbeefcafe0 is hex but 1234567 is a number", ["1234567"]),
    ],
)
def test_scanner_finds_numbers(text: str, tokens: list[str]) -> None:
    assert [t for _, t in claims.find_numbers(text, TOP)] == tokens


# ------------------------------------------------------------------ selectors

DOC: dict[str, Any] = {
    "estimates": [
        {"monitor": "rule", "prevalence": 0.001, "budget": 20, "estimate": 0.41, "ok": True},
        {"monitor": "rule", "prevalence": 0.0001, "budget": 20, "estimate": 0.39, "ok": False},
        {"monitor": "llm", "prevalence": 0.001, "budget": 20, "estimate": 0.55, "ok": None},
    ],
    "by_prevalence": {"0.001": {"n": 7}},
    "commit": "9330c9d396b3",
    "rows": [[1, 2], [3, 4]],
}


@pytest.mark.parametrize(
    ("expr", "value"),
    [
        ("estimates[monitor=rule,prevalence=1e-3].estimate", 0.41),
        ("estimates[monitor=rule,prevalence=0.0001].estimate", 0.39),
        ("estimates[ok=true].monitor", "rule"),
        ("estimates[ok=null].monitor", "llm"),
        ("estimates[-1].estimate", 0.55),
        ('by_prevalence["0.001"].n', 7),
        ("rows[1][0]", 3),
        ("estimates|len", 3),
        ("estimates|count:monitor=rule", 2),
        ("estimates|count:monitor=rule,budget=20", 2),
        ("commit|head:7", "9330c9d"),
        ("", DOC),
    ],
)
def test_selector_resolves(expr: str, value: Any) -> None:
    assert claims.select(DOC, claims.parse_selector(expr)) == value


@pytest.mark.parametrize(
    ("expr", "needle"),
    [
        ("estimates[monitor=rule].estimate", "matches 2 elements"),
        ("estimates[monitor=none].estimate", "matches 0 elements"),
        ("estimates[9]", "no element [9]"),
        ("missing", "no key 'missing'"),
        ("commit.x", "no key 'x'"),
        ("commit|len", None),
        ("estimates|head:3", "head applies to a string"),
        ("estimates[0].estimate|len", "len applies to"),
        ("commit[monitor=x]", "applies to a str, not a list"),
    ],
)
def test_selector_errors(expr: str, needle: str | None) -> None:
    sel = claims.parse_selector(expr)
    if needle is None:
        assert claims.select(DOC, sel) == len(DOC["commit"])
        return
    with pytest.raises(claims.ClaimError, match=needle.replace("[", r"\[").replace("]", r"\]")):
        claims.select(DOC, sel)


@pytest.mark.parametrize(
    "expr",
    [".a", "a..b", "a.", "a.[0]", "a[0]b", "a[", "a[x]", "a|sum", "a|head:0", '["unclosed', "a b"],
)
def test_selector_syntax_errors(expr: str) -> None:
    with pytest.raises(claims.ClaimError):
        claims.parse_selector(expr)


# -------------------------------------------------------------------- formats


@pytest.mark.parametrize(
    ("fmt", "value", "text"),
    [
        ("{:.3f}", 0.41234, "0.412"),
        ("{:,d}", 21212, "21,212"),
        ("{:.1%}", 0.4123, "41.2%"),
        ("{:g}", 0.0001, "0.0001"),
        ("{}", "rule-pack", "rule-pack"),
        ("[{lo:.2f}, {hi:.2f}]", {"lo": 0.301, "hi": 0.529}, "[0.30, 0.53]"),
    ],
)
def test_formats_render(fmt: str, value: Any, text: str) -> None:
    assert claims.render_value(value, claims.parse_format(fmt)) == text


@pytest.mark.parametrize(
    ("fmt", "needle"),
    [
        ("{} of 3", "digits outside its fields"),
        ("at 95%: {}", "digits outside its fields"),
        ("{!r}", "conversions"),
        ("{0}", "must be {} or a plain key"),
        ("{a.b}", "must be {} or a plain key"),
        ("{:{w}}", "nested fields"),
        ("plain text", "has no {} field"),
        ("{", "format '{'"),
    ],
)
def test_format_rules(fmt: str, needle: str) -> None:
    with pytest.raises(claims.ClaimError, match=needle.replace("{", r"\{").replace("}", r"\}")):
        claims.parse_format(fmt)


@pytest.mark.parametrize(
    ("value", "fmt", "needle"),
    [
        (True, "{}", "is a bool"),
        (None, "{}", "is a NoneType"),
        (float("nan"), "{}", "is nan"),
        ([1], "{}", "is a list"),
        ({"a": 1}, "{b}", "has no key 'b'"),
        ("x", "{:.3f}", "cannot format"),
        ("two\nlines", "{}", "one non-empty line"),
        ("{{x}}", "{}", "one non-empty line"),
    ],
)
def test_render_value_rejects(value: Any, fmt: str, needle: str) -> None:
    with pytest.raises(claims.ClaimError, match=needle):
        claims.render_value(value, claims.parse_format(fmt))


# ---------------------------------------------------------- matcher, allowances


def test_decompose_survives_a_value_that_contains_the_next_literal() -> None:
    template = claims.parse_template("t", "Range {{claim:a}}, then {{claim:b}}.\n")
    doc = "Range 1, 2, then 3.\n"
    pieces = template.pieces()
    spans = claims.decompose(doc, pieces, claims.options_ends(doc, {"a": ["1, 2"], "b": ["3"]}))
    assert not isinstance(spans, claims.Mismatch)
    assert [doc[s:e] for s, e in spans] == ["Range ", "1, 2", ", then ", "3", ".\n"]


def test_decompose_handles_adjacent_claims() -> None:
    template = claims.parse_template("t", "{{claim:a}}{{claim:b}}")
    pieces = template.pieces()
    ends = claims.options_ends("0.50.6", {"a": ["0.5", "0.50"], "b": ["0.6", ".6"]})
    spans = claims.decompose("0.50.6", pieces, ends)
    assert not isinstance(spans, claims.Mismatch)
    assert len(spans) == 2


def test_allowances_expand_into_literal_pieces() -> None:
    template = claims.parse_template("t", "At {{param:1e-3}} recall {{claim:r}} ({{param:95%}}).")
    assert [(p.text, p.claim) for p in template.pieces()] == [
        ("At 1e-3 recall ", False),
        ("r", True),
        (" (95%).", False),
    ]
    assert template.render({"r": "0.41"}) == "At 1e-3 recall 0.41 (95%)."


@pytest.mark.parametrize(
    ("kind", "text", "ok"),
    [
        ("date", "2026", True),
        ("date", "2026-10", True),
        ("date", "2026-10-23", True),
        ("date", "1850", False),
        ("date", "2026-00", False),
        ("version", "3.11", True),
        ("version", "v1.0", True),
        ("version", "0.14.1", True),
        ("version", "2.1.309", True),
        ("version", "v2", True),
        ("version", "3", False),
        ("name", "qwen2.5:1.5b", True),
        ("label", "section 10.1", True),
        ("param", "1e-2, 1e-3, and 1e-4", True),
        ("cap", "300", True),
        ("threshold", "0.5", True),
    ],
)
def test_allowance_rules(kind: str, text: str, ok: bool) -> None:
    def brief() -> str:
        return ""

    assert (claims.allowance_problem(kind, text, brief) is None) is ok


def test_source_paths_are_restricted() -> None:
    claims.check_source_path("artifacts/e2/results.json")
    claims.check_source_path("prereg/0001-e2-main.md")
    for bad in (
        "results.json",
        "artifacts/../x.json",
        "/abs/a.json",
        "artifacts/a.csv",
        "docs/x.md",
    ):
        with pytest.raises(claims.ClaimError):
            claims.check_source_path(bad)
