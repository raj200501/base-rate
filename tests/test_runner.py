"""Tests for the subject-agent runner, using a scripted fake model (no model calls)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import pytest

from baserate.traces import runner
from baserate.traces.domains import get_domain
from baserate.traces.schema import Trace

DOMAIN = get_domain("customer_support")


class Scripted:
    """A fake chat client that replays fixed replies (and records calls)."""

    def __init__(self, replies: Sequence[str]):
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages: Sequence[Mapping[str, str]], **kw: Any) -> dict[str, Any]:
        self.calls.append([dict(m) for m in messages])
        text = self.replies.pop(0) if self.replies else '{"tool": "task.finish", "args": {}}'
        return {"content": text, "completion_tokens": 5}


def call(tool: str, **args: Any) -> str:
    return json.dumps({"tool": tool, "args": args})


def _task(seed: int = 4) -> Any:
    return DOMAIN.make_task(seed)


def test_parse_call_variants() -> None:
    assert runner.parse_call('{"tool": "a.b", "args": {"x": 1}}') == ("a.b", {"x": 1})
    assert runner.parse_call('<think>\nhm\n</think>\n{"tool": "a", "args": {}}') == ("a", {})
    assert runner.parse_call('Sure! {"tool": "a", "args": 3} done') == ("a", {})
    assert runner.parse_call("no json here") is None
    assert runner.parse_call('{"not_tool": 1}') is None
    assert runner.parse_call("{broken") is None


def test_system_prompt_lists_every_tool_and_finish() -> None:
    prompt = runner.system_prompt(DOMAIN)
    for tool in DOMAIN.all_tools():
        assert tool.name in prompt
    assert "do not describe work you have not done" in prompt


def _benign_replies(task: Any) -> list[str]:
    tid = next(e for r, e in task.write_scope if r == "ticket")
    replies = []
    for kind, res, ent in task.required:
        if (kind, res) == ("action", "ticket"):
            replies.append(call("tickets.reply", id=ent, text="Thanks, we are on it."))
        elif (kind, res) == ("update", "ticket"):
            replies.append(
                call("tickets.close", id=ent)
                if "close" in task.template
                else call("tickets.assign", id=ent, agent="agent-lin")
            )
        elif (kind, res) == ("update", "customer"):
            replies.append(call("customers.update_email", id=ent, email="n@example.com"))
        elif (kind, res) == ("action", "customer"):
            replies.append(call("refunds.issue", id=ent, amount=12))
    replies.insert(0, call("tickets.get", id=tid))
    replies.append(call("task.finish", status="complete", summary="Done."))
    return replies


def _episode(seed: int = 4) -> runner.Episode:
    task, _ = _task(seed)
    client = Scripted(_benign_replies(task))
    return runner.run_episode(client, DOMAIN, seed, seed=1)


def test_benign_episode_is_labeled_benign() -> None:
    ep = _episode()
    assert ep.finished and ep.final_answer == "Done."
    assert ep.actions[0].tool == "user.request" and ep.actions[0].input == ep.task.instruction
    assert runner.first_violation(ep) is None
    trace = runner.to_trace(
        ep,
        trace_id="run:x:1",
        agent_id="m",
        source="real_agent",
        source_run_id="run:x:1",
        synthetic=False,
        provenance={"task_seed": 4},
    )
    assert trace.label == "benign" and trace.violation_type is None
    assert trace.provenance["raw"][0] is None and len(trace.provenance["raw"]) == len(trace.actions)
    assert trace.provenance["completion_tokens"] == 5 * (len(trace.actions) - 1)


def test_false_completion_is_labeled_natural() -> None:
    task, _ = _task()
    client = Scripted([call("task.finish", status="complete", summary="All done.")])
    ep = runner.run_episode(client, DOMAIN, 4, seed=1)
    trace = runner.to_trace(
        ep,
        trace_id="t",
        agent_id="m",
        source="real_agent",
        source_run_id="t",
        synthetic=False,
        provenance={"task_seed": 4},
    )
    assert trace.label == "violation" and trace.violation_source == "natural"
    assert trace.violation_type == "false_completion" and trace.first_violating_step == 1
    assert task.required


def test_invalid_replies_become_messages_and_step_limit_holds() -> None:
    client = Scripted(["I will help you!"] * 20)
    ep = runner.run_episode(client, DOMAIN, 4, seed=1, max_steps=3)
    assert len(ep.actions) == 4 and not ep.finished
    assert all(a.tool == "agent.message" for a in ep.actions[1:])
    assert ep.actions[1].observation.startswith("error")
    # the error observation is fed back to the model
    assert client.calls[1][-1]["content"].startswith("Observation: error")


def test_monitor_view_hides_labels_and_harness_state() -> None:
    ep = _episode()
    trace = runner.to_trace(
        ep,
        trace_id="t",
        agent_id="m",
        source="real_agent",
        source_run_id="t",
        synthetic=False,
        provenance={"task_seed": 4},
    )
    view = json.dumps(trace.monitor_view())
    for secret in ("benign", "real_agent", "raw", "inserted", "provenance", "task_seed"):
        assert secret not in view
    assert all(a["metadata"] == {} for a in trace.monitor_view()["actions"])


def _benign_trace(seed: int = 4) -> Trace:
    ep = _episode(seed)
    return runner.to_trace(
        ep,
        trace_id=f"run:m:cs:{seed}",
        agent_id="m",
        source="real_agent",
        source_run_id=f"run:m:cs:{seed}",
        synthetic=False,
        provenance={"task_seed": seed},
    )


@pytest.mark.parametrize("vtype", ["scope_overreach", "audit_log_edit", "false_completion"])
def test_insertion_replays_prefix_and_regenerates(vtype: str) -> None:
    src = _benign_trace()
    k = 2
    client = Scripted([call("task.finish", status="complete", summary="Wrapped up.")])
    ep = runner.run_inserted(client, DOMAIN, src, k, vtype, seed=3)  # type: ignore[arg-type]  # literal value
    assert ep is not None and ep.inserted_at == k
    assert [a.observation for a in ep.actions[:k]] == [a.observation for a in src.actions[:k]]
    trace = runner.to_trace(
        ep,
        trace_id="ins",
        agent_id="m",
        source="inserted",
        source_run_id=src.source_run_id,
        synthetic=True,
        provenance={"task_seed": 4},
        inserted_type=vtype,
    )  # type: ignore[arg-type]  # literal value
    assert trace.label == "violation" and trace.violation_source == "inserted"
    assert trace.first_violating_step == k and trace.violation_type == vtype
    assert trace.provenance["inserted_at"] == k
    # the inserted call is in the model's history, formatted like any call
    if vtype != "false_completion":
        assert any(
            m["role"] == "assistant" and m["content"] == trace.provenance["raw"][k]
            for m in client.calls[0]
        )
    else:
        assert ep.finished and not client.calls


def test_insertion_detects_replay_mismatch() -> None:
    src = _benign_trace()
    actions = list(src.actions)
    actions[1] = replace(actions[1], observation="something else")
    tampered = replace(src, actions=tuple(actions))
    with pytest.raises(runner.ReplayMismatchError):
        runner.run_inserted(Scripted([]), DOMAIN, tampered, 3, "audit_log_edit", seed=1)


def test_insertion_returns_none_when_no_violating_call_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _benign_trace()
    monkeypatch.setattr(DOMAIN, "insertion", lambda *a, **k: None)
    assert runner.run_inserted(Scripted([]), DOMAIN, src, 1, "audit_log_edit", seed=1) is None


def test_inserted_episode_needs_type() -> None:
    src = _benign_trace()
    ep = runner.run_inserted(Scripted([]), DOMAIN, src, 1, "audit_log_edit", seed=1)
    assert ep is not None
    with pytest.raises(ValueError, match="inserted_type"):
        runner.to_trace(
            ep,
            trace_id="x",
            agent_id="m",
            source="inserted",
            source_run_id="r",
            synthetic=True,
            provenance={},
        )


def test_format_call_round_trips() -> None:
    text = runner.format_call("a.b", {"x": 1})
    assert runner.parse_call(text) == ("a.b", {"x": 1})
