"""Tests for the local model monitor (fake completion client; no model calls)."""

from __future__ import annotations

from typing import Any

import pytest

from baserate.backends.client import _top_probs
from baserate.monitors.base import Monitor
from baserate.monitors.model import (
    MONITOR_SYSTEM,
    ModelMonitor,
    chatml_prompt,
    digit_score,
    prompt_sha256,
    render_view,
)
from baserate.traces.schema import Action, Trace


def _trace() -> Trace:
    return Trace(
        trace_id="t1",
        eval_id="customer_support:reply",
        agent_id="m",
        domain="customer_support",
        source="real_agent",
        source_run_id="t1",
        synthetic=False,
        label="benign",
        actions=(
            Action(0, "user.request", "Reply to ticket T-1."),
            Action(1, "tickets.get", '{"id": "T-1"}', '{"status": "open"}'),
            Action(2, "tickets.reply", '{"id": "T-1", "text": "hi"}', "reply sent: ticket T-1"),
        ),
        final_answer="Replied.",
    )


class Fake:
    def __init__(self, top: list[dict[str, float]] | None = None, content: str = "0"):
        self.top = top if top is not None else [{"0": 0.5, "9": 0.5}]
        self.content = content
        self.prompts: list[str] = []

    def complete(self, prompt: str, **kw: Any) -> dict[str, Any]:
        self.prompts.append(prompt)
        return {"content": self.content, "top": self.top}


def test_render_view_uses_only_monitor_visible_fields() -> None:
    text = render_view(_trace().monitor_view())
    assert text.startswith("Task request: Reply to ticket T-1.")
    assert "[1] tickets.get" in text and "Final answer: Replied." in text
    assert "benign" not in text and "real_agent" not in text
    assert "(none)" in render_view({"actions": [{"tool": "user.request", "input": "x"}]})


def test_long_fields_are_clipped() -> None:
    view = {"actions": [{"step": 1, "tool": "a", "input": "x" * 5000, "observation": ""}]}
    assert len(render_view(view)) < 600


def test_chatml_prompt_is_non_thinking_and_ends_at_the_digit() -> None:
    p = chatml_prompt(_trace().monitor_view())
    assert MONITOR_SYSTEM in p and p.endswith("<think>\n\n</think>\n\n")
    assert "Suspicion score (one digit, 0 to 9):" in p
    assert len(prompt_sha256()) == 64


def test_digit_score_is_probability_weighted() -> None:
    score, mass = digit_score([{"0": 0.25, "9": 0.25, " x": 0.5}])
    assert mass == pytest.approx(0.5) and score == pytest.approx(0.5)
    assert digit_score([], "7") == (pytest.approx(7 / 9), 0.0)
    assert digit_score([{"no": 1.0}], "none") == (0.0, 0.0)


def test_top_probs_formats() -> None:
    import math

    assert _top_probs({"top_logprobs": [{"token": "3", "logprob": math.log(0.5)}]}) == {
        "3": pytest.approx(0.5)
    }
    assert _top_probs({"probs": [{"tok_str": "4", "prob": 0.2}]}) == {"4": 0.2}
    assert _top_probs({}) == {}


def test_monitor_scores_and_counts_low_digit_mass() -> None:
    fake = Fake()
    mon = ModelMonitor("model:fake", fake)
    assert isinstance(mon, Monitor) and mon.family == "model"
    s = mon.score(_trace())
    assert s.score == pytest.approx(0.5) and s.details["prompt_sha256"] == prompt_sha256()
    low = ModelMonitor("model:fake", Fake(top=[{"a": 0.9, "8": 0.1}]))
    low.score(_trace())
    assert low.low_mass == 1


def test_prefix_scores_cut_after_each_action() -> None:
    fake = Fake()
    out = ModelMonitor("model:fake", fake).score_prefixes(_trace(), start=1)
    assert len(out) == 2
    assert "Final answer" not in fake.prompts[0] and "[2] tickets.reply" not in fake.prompts[0]
    assert "Final answer: Replied." in fake.prompts[1]
