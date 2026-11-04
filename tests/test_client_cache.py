"""The model client's response cache under concurrent identical requests.

Monitor scoring sends several requests at once, and identical monitor views (for
example duplicate records in the Armour set) produce identical requests. Each
writer must use its own temporary file, or one writer's rename removes the file
another writer is about to rename.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import baserate.backends.client as client_mod

REPO = Path(__file__).resolve().parents[1]
WORKERS = 8
ROUNDS = 20


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> client_mod.ModelClient:
    monkeypatch.delenv("BASERATE_NO_MODEL_CALLS", raising=False)
    barrier = threading.Barrier(WORKERS)

    def fake_post(url: str, payload: Any, timeout: float) -> dict[str, Any]:
        barrier.wait(timeout=30)  # every worker holds a reply to the same request at once
        if url.endswith("/completion"):
            return {"content": "3", "tokens_evaluated": 5, "tokens_predicted": 1}
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }

    monkeypatch.setattr(client_mod, "post_json", fake_post)
    spec = client_mod.ModelSpec("fake-model", 0.6)
    return client_mod.ModelClient(spec, REPO, cache_dir=tmp_path / "cache")


@pytest.mark.parametrize("call", ["complete", "chat"])
def test_concurrent_identical_requests_all_succeed_and_leave_one_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    client = _client(tmp_path, monkeypatch)

    def request(round_index: int) -> dict[str, Any]:
        if call == "complete":
            return client.complete(f"prompt {round_index}")
        return client.chat([{"role": "user", "content": f"prompt {round_index}"}])

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for r in range(ROUNDS):
            outs = list(pool.map(request, [r] * WORKERS))
            assert all(o["cached"] is False for o in outs)
    cache = tmp_path / "cache"
    assert sorted(p.suffix for p in cache.rglob("*") if p.is_file()) == [".json"] * ROUNDS
    assert client.usage.calls == WORKERS * ROUNDS
    assert request(0)["cached"] is True  # the entry is readable afterwards
