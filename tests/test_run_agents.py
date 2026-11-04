"""Tests for the controlled-insertion draw in tools/run_agents.py (prereg/0007).

Sources are the real runs benign under the fixed oracle (relabeled by replay,
never by the stored generation label), sorted by trace id, and the plan is a
function of the sorted sources and the insertion seed only.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from baserate.traces import runner
from baserate.traces.domains import get_domain
from baserate.traces.schema import VIOLATION_TYPES, Trace

REPO = Path(__file__).resolve().parents[1]
DOMAIN = get_domain("customer_support")


def _load_tool(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", REPO / f"tools/{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "run_agents_under_test", REPO / "tools/run_agents.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_agents_under_test"] = module
    spec.loader.exec_module(module)
    return module


ra = _load()


class Scripted:
    """A fake chat client that replays fixed replies."""

    def __init__(self, replies: Sequence[str]) -> None:
        self.replies = list(replies)

    def chat(self, messages: Sequence[Mapping[str, str]], **kw: Any) -> dict[str, Any]:
        text = self.replies.pop(0) if self.replies else '{"tool": "task.finish", "args": {}}'
        return {"content": text, "completion_tokens": 5}


def call(tool: str, **args: Any) -> str:
    return json.dumps({"tool": tool, "args": args})


def run(seed: int, replies: list[str], model: str = "m") -> Trace:
    ep = runner.run_episode(Scripted(replies), DOMAIN, seed, seed=1)
    tid = f"run:{model}:customer_support:{seed}"
    return runner.to_trace(
        ep,
        trace_id=tid,
        agent_id=model,
        source="real_agent",
        source_run_id=tid,
        synthetic=False,
        provenance={"task_seed": seed},
    )


def finish() -> str:
    return call("task.finish", status="failed", summary="Stopped.")


def corpus() -> dict[str, Trace]:
    """Runs of several kinds; only the stale-label and plain benign runs are eligible."""
    plain = run(4, [call("tickets.get", id="T-0000"), finish()])
    # a call on a missing entity fails and changes nothing: benign under the fixed
    # oracle; it is stored here with the old oracle's violation label
    missing = run(5, [call("tickets.close", id="T-9999"), finish()])
    assert missing.label == "benign"
    stale = replace(
        missing,
        label="violation",
        violation_type="scope_overreach",
        violation_source="natural",
        first_violating_step=1,
    )
    exported = run(6, [call("customers.export_all"), finish()])
    assert exported.label == "violation"
    other_model = run(7, [call("tickets.get", id="T-0000"), finish()], model="other")
    return {"plain": plain, "stale": stale, "exported": exported, "other": other_model}


def write(tmp: Path, name: str, traces: list[Trace]) -> Path:
    path = tmp / name
    path.write_text("".join(json.dumps(t.to_dict()) + "\n" for t in traces))
    return path


def test_sources_use_the_fixed_oracle_not_the_stored_label(tmp_path: Path) -> None:
    c = corpus()
    path = write(tmp_path, "a.jsonl", list(c.values()))
    found = ra.insertion_sources([path], "m")
    assert [t.trace_id for t in found] == sorted([c["plain"].trace_id, c["stale"].trace_id])


def test_sources_and_plan_do_not_depend_on_file_order(tmp_path: Path) -> None:
    traces = [run(s, [call("tickets.get", id="T-0000"), finish()]) for s in range(10, 22)]
    forward = write(tmp_path, "f.jsonl", traces)
    backward = write(tmp_path, "b.jsonl", traces[::-1])
    split = [write(tmp_path, "s1.jsonl", traces[6:]), write(tmp_path, "s2.jsonl", traces[:6])]
    a = ra.insertion_sources([forward], "m")
    b = ra.insertion_sources([backward], "m")
    c = ra.insertion_sources(split, "m")
    assert [t.trace_id for t in a] == [t.trace_id for t in b] == [t.trace_id for t in c]
    plan_a = [(s.trace_id, k, v) for s, k, v in ra.insertion_plan(a, 7, 20261023)]
    plan_b = [(s.trace_id, k, v) for s, k, v in ra.insertion_plan(b, 7, 20261023)]
    assert plan_a == plan_b
    assert plan_a != [(s.trace_id, k, v) for s, k, v in ra.insertion_plan(a, 7, 1)]


def test_plan_is_one_insertion_per_source_with_cycling_types_and_valid_steps(
    tmp_path: Path,
) -> None:
    traces = [run(s, [call("tickets.get", id="T-0000"), finish()]) for s in range(30, 42)]
    sources = ra.insertion_sources([write(tmp_path, "x.jsonl", traces)], "m")
    plan = ra.insertion_plan(sources, 11, 20261023)
    assert len(plan) == 11 and len({s.trace_id for s, _, _ in plan}) == 11
    assert [v for _, _, v in plan] == [VIOLATION_TYPES[i % 5] for i in range(11)]
    assert all(1 <= k <= len(s.actions) - 1 for s, k, _ in plan)
    assert ra.insertion_plan(sources, 0, 20261023) == []


def test_insertion_seed_comes_from_the_frozen_e1_config() -> None:
    assert ra.e1_config()["insertion"]["seed"] == 20261023


def test_repair_cuts_only_a_partial_last_record(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    path.write_text('{"trace_id": "a"}\n{"trace_id": "b"}\n{"trace_id": "c", "act')
    assert ra.repair_jsonl(path) == len('{"trace_id": "c", "act')
    assert ra.done_ids(path) == {"a", "b"}
    assert ra.repair_jsonl(path) == 0
    path.write_text('{"trace_id": "a"}\nnot json\n{"trace_id": "b"}\n')
    with pytest.raises(json.JSONDecodeError):
        ra.repair_jsonl(path)
    assert ra.repair_jsonl(tmp_path / "missing.jsonl") == 0


def test_server_start_refuses_a_port_that_already_answers(tmp_path: Path) -> None:
    import http.server
    import threading

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with pytest.raises(SystemExit, match="already serves"):
            ra.start_server({"file": "x.gguf"}, httpd.server_port, 1, 1, tmp_path / "s.log")
    finally:
        httpd.shutdown()


def test_split_rounds_the_second_share_half_up() -> None:
    cb = _load_tool("count_benign")
    assert cb.split_attempts(700, 1600, 500) == (533, 167)  # 166.67 -> 167
    assert cb.split_attempts(700, 1, 1) == (350, 350)
    assert cb.split_attempts(7, 1, 1) == (3, 4)  # 3.5 rounds up
    assert cb.split_attempts(700, 0, 0) == (700, 0)


def test_client_retries_a_dropped_connection_but_not_a_client_error() -> None:
    import http.server
    import threading
    import urllib.error

    from baserate.backends import client as client_mod

    calls = {"n": 0}

    class Flaky(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            calls["n"] += 1
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/bad":
                self.send_response(400)
                self.end_headers()
                return
            if calls["n"] == 1:
                self.close_connection = True
                self.wfile.flush()
                self.connection.close()  # drop the first request without a reply
                return
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Flaky)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    old = client_mod.RETRY_DELAYS
    client_mod.RETRY_DELAYS = (0.0, 0.0, 0.0)
    try:
        url = f"http://127.0.0.1:{httpd.server_port}"
        assert client_mod.post_json(url + "/ok", {"x": 1}, 5) == {"ok": True}
        assert calls["n"] == 2
        calls["n"] = 10
        with pytest.raises(urllib.error.HTTPError):
            client_mod.post_json(url + "/bad", {"x": 1}, 5)
        assert calls["n"] == 11  # a client error is not retried
    finally:
        client_mod.RETRY_DELAYS = old
        httpd.shutdown()


def test_a_failed_job_cancels_the_queued_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    ran: list[str] = []

    def boom(*args: Any, **kwargs: Any) -> Any:
        ran.append(args[2].trace_id)
        raise RuntimeError("server gone")

    class Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def flush(self) -> None:
            pass

    monkeypatch.setattr(ra.runner, "run_inserted", boom)
    monkeypatch.setattr(ra, "ModelClient", Client)
    sources = [run(s, [call("tickets.get", id="T-0000"), finish()]) for s in range(60, 72)]
    plan = ra.insertion_plan(sources, 12, 20261023)
    args = argparse.Namespace(out=tmp_path, insert=True, concurrency=1, sample_seed=1, model="m")
    meta = {"model": "m", "revision": "r", "sha256": "s", "sample_seed": 1, "git_sha": "g"}
    with pytest.raises(RuntimeError, match="server gone"):
        ra._run(
            args, meta, None, None, plan, tmp_path / "t.jsonl", tmp_path / "s.jsonl", set(), 0, 0
        )
    assert len(ran) == 1  # the other eleven jobs were cancelled, not run
