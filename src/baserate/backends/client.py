"""The one client wrapper every model call goes through (brief section 8).

It enforces the usage caps in budget.toml, refuses Claude children while the
billing guard finds a problem, refuses all model calls when
``BASERATE_NO_MODEL_CALLS=1`` (the offline demo and ship-check set it),
caches responses by content hash so a banked call is never re-run, and logs
usage to ``artifacts/usage/ledger.jsonl``.

Local calls are logged once per session of work (:meth:`ModelClient.flush`
writes one ``local_call`` entry with call, token, and wall-time totals) to
keep the ledger small; Claude calls would be logged one entry each.

Backends: ``llamacpp`` talks to a local llama-server through its
OpenAI-compatible chat endpoint. There is no paid API backend.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from baserate import budget as budget_mod
from baserate.guard import current_problems

RETRY_DELAYS = (2.0, 4.0, 8.0)
"""Seconds to wait before each retry of a request that failed in transit."""


def _transient(exc: BaseException) -> bool:
    """A failure in transit (connection refused or dropped, timeout, server busy)."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {502, 503, 504}
    return isinstance(
        exc, urllib.error.URLError | http.client.HTTPException | ConnectionError | TimeoutError
    )


def post_json(url: str, payload: Mapping[str, Any], timeout: float) -> dict[str, Any]:
    """POST JSON and parse the reply, retrying failures in transit a few times.

    Requests are deterministic (fixed seed), so a retry asks for the same
    reply; a server that stays down still fails after the last retry.
    """
    body = json.dumps(payload).encode()
    for attempt in range(len(RETRY_DELAYS) + 1):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data: dict[str, Any] = json.loads(resp.read())
                return data
        except Exception as exc:
            if attempt == len(RETRY_DELAYS) or not _transient(exc):
                raise
            time.sleep(RETRY_DELAYS[attempt])
    raise AssertionError("unreachable")


Message = Mapping[str, str]


class ModelCallRefusedError(RuntimeError):
    """A model call was refused (offline mode, billing guard, or cap)."""


@dataclass(frozen=True)
class ModelSpec:
    """A pinned model served locally."""

    key: str
    params_b: float
    endpoint: str = "http://127.0.0.1:8091"


@dataclass
class Usage:
    """Running totals for the ledger."""

    calls: int = 0
    cached: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0


@dataclass
class ModelClient:
    """Cached, capped, logged chat completions.

    Attributes:
        model: The model being called.
        root: Repository root (for budget.toml and the ledger).
        cache_dir: Response cache; default ``runs/cache`` under root.
        purpose: Short label written to the ledger (for example ``agent-runs``).
    """

    model: ModelSpec
    root: Path
    cache_dir: Path | None = None
    purpose: str = ""
    usage: Usage = field(default_factory=Usage)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if os.environ.get("BASERATE_NO_MODEL_CALLS") == "1":
            raise ModelCallRefusedError("BASERATE_NO_MODEL_CALLS=1: model calls are disabled")
        caps = budget_mod.load_budget(self.root / "budget.toml")
        if self.model.params_b > caps.caps["local_model_max_params_b"]:
            raise ModelCallRefusedError(
                f"{self.model.key} has {self.model.params_b}B parameters, above the cap"
            )
        if self.cache_dir is None:
            self.cache_dir = self.root / "runs" / "cache" / self.model.key
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def request_key(model_key: str, payload: Mapping[str, Any]) -> str:
        """Content hash identifying a request."""
        blob = json.dumps({"model": model_key, **payload}, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()

    def chat(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 200,
        temperature: float = 0.7,
        top_p: float = 0.8,
        seed: int = 0,
        timeout: float = 600.0,
    ) -> dict[str, Any]:
        """One chat completion.

        Returns:
            ``content``, ``prompt_tokens``, ``completion_tokens``, and ``cached``.
        """
        payload = {
            "messages": [dict(m) for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": 20,
            "seed": seed,
        }
        key = self.request_key(self.model.key, payload)
        assert self.cache_dir is not None
        path = self.cache_dir / key[:2] / f"{key}.json"
        if path.exists():
            out: dict[str, Any] = json.loads(path.read_text())
            with self._lock:
                self.usage.cached += 1
            return {**out, "cached": True}
        start = time.monotonic()
        data = post_json(f"{self.model.endpoint}/v1/chat/completions", payload, timeout)
        elapsed = time.monotonic() - start
        msg = data["choices"][0]["message"]
        out = {
            "content": str(msg.get("content") or ""),
            "prompt_tokens": int(data.get("usage", {}).get("prompt_tokens", 0)),
            "completion_tokens": int(data.get("usage", {}).get("completion_tokens", 0)),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(out))
        tmp.replace(path)
        with self._lock:
            self.usage.calls += 1
            self.usage.prompt_tokens += out["prompt_tokens"]
            self.usage.completion_tokens += out["completion_tokens"]
            self.usage.seconds += elapsed
        return {**out, "cached": False}

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 1,
        n_probs: int = 0,
        temperature: float = 0.0,
        seed: int = 0,
        timeout: float = 600.0,
    ) -> dict[str, Any]:
        """One raw completion (llama-server ``/completion``), optionally with token probabilities.

        Returns:
            ``content``, ``top`` (list per generated token of ``{token: prob}``),
            ``prompt_tokens``, ``completion_tokens``, and ``cached``.
        """
        payload: dict[str, Any] = {
            "prompt": prompt,
            "n_predict": max_tokens,
            "temperature": temperature,
            "seed": seed,
            "n_probs": n_probs,
            "cache_prompt": True,
        }
        key = self.request_key(self.model.key + ":completion", payload)
        assert self.cache_dir is not None
        path = self.cache_dir / key[:2] / f"{key}.json"
        if path.exists():
            out: dict[str, Any] = json.loads(path.read_text())
            with self._lock:
                self.usage.cached += 1
            return {**out, "cached": True}
        start = time.monotonic()
        data = post_json(f"{self.model.endpoint}/completion", payload, timeout)
        elapsed = time.monotonic() - start
        out = {
            "content": str(data.get("content", "")),
            "top": [_top_probs(item) for item in data.get("completion_probabilities", [])],
            "prompt_tokens": int(data.get("tokens_evaluated", 0)),
            "completion_tokens": int(data.get("tokens_predicted", 0)),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(out))
        tmp.replace(path)
        with self._lock:
            self.usage.calls += 1
            self.usage.prompt_tokens += out["prompt_tokens"]
            self.usage.completion_tokens += out["completion_tokens"]
            self.usage.seconds += elapsed
        return {**out, "cached": False}

    def flush(self) -> dict[str, Any] | None:
        """Append one ``local_call`` ledger entry with the totals since the last flush."""
        with self._lock:
            u, self.usage = self.usage, Usage()
        if u.calls == 0 and u.cached == 0:
            return None
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "kind": "local_call",
            "backend": "llamacpp",
            "model": self.model.key,
            "params_b": self.model.params_b,
            "purpose": self.purpose,
            "calls": u.calls,
            "cached_calls": u.cached,
            "prompt_tokens": u.prompt_tokens,
            "completion_tokens": u.completion_tokens,
            "wall_seconds": round(u.seconds, 1),
            "cash_usd": 0,
            "plan_equivalent_usd": 0,
            "cap_share": float(os.environ.get("CAP_SHARE", "1")),
        }
        ledger = self.root / "artifacts" / "usage" / "ledger.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry


def claude_allowed(root: Path) -> list[str]:
    """Reasons Claude children may not run here (empty means allowed)."""
    reasons = list(current_problems(root))
    if os.environ.get("BASERATE_NO_MODEL_CALLS") == "1":
        reasons.append("BASERATE_NO_MODEL_CALLS=1")
    return reasons


def _top_probs(item: Mapping[str, Any]) -> dict[str, float]:
    """Token to probability for one generated position, across llama-server formats."""
    import math

    entries = item.get("top_logprobs") or item.get("top_probs") or item.get("probs") or []
    out: dict[str, float] = {}
    for e in entries:
        tok = str(e.get("token", e.get("tok_str", "")))
        if "prob" in e:
            out[tok] = float(e["prob"])
        elif "logprob" in e:
            out[tok] = math.exp(float(e["logprob"]))
    return out
