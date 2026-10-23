"""Structural checks every mock-tool domain must pass (used by the test suite)."""

from __future__ import annotations

import random
from typing import Any, cast

from baserate.traces.domains.base import Domain, classify
from baserate.traces.schema import VIOLATION_TYPES, ViolationType

MUTATING = frozenset({"update", "action", "create", "delete"})


def check_domain(domain: Domain, seeds: int = 20, min_insert_share: float = 0.75) -> None:
    """Raise AssertionError if ``domain`` breaks a framework invariant.

    Checks: tasks are deterministic per seed and cover every template; every
    task has a write scope and completion requirements inside it; every tool
    returns an observation string; doing exactly the required work and
    finishing ``complete`` violates nothing; and each of the five violation
    types can be inserted for at least ``min_insert_share`` of seeds, with the
    oracle agreeing on the type.
    """
    for seed in range(seeds):
        a, wa = domain.make_task(seed)
        b, wb = domain.make_task(seed)
        assert a == b and wa.store == wb.store, "tasks must be deterministic per seed"
        assert a.domain == domain.name and a.task_id.startswith(f"{domain.name}:")
        assert a.write_scope and a.required, f"{a.task_id}: needs write scope and requirements"
        for kind, res, ent in a.required:
            assert kind not in MUTATING or (res, ent) in a.write_scope, a.task_id
    seen = {domain.make_task(s)[0].template for s in range(10 * len(domain.templates) + 50)}
    assert seen == set(domain.templates), "every template must be reachable"
    for tool in domain.all_tools():
        _, world = domain.make_task(1)
        obs, _ = world.call(tool.name, {p: "x" for p in tool.required_params()})
        assert isinstance(obs, str) and obs, tool.name
    for seed in range(seeds):
        task, world = domain.make_task(seed)
        for kind, res, ent in task.required:
            tool = next(
                (
                    t
                    for t in domain.all_tools()
                    if t.kind == kind and t.resource == res and not t.sensitive
                ),
                None,
            )
            assert tool is not None, f"no tool performs required {kind} on {res}"
            args: dict[str, Any] = {p: "value" for p in tool.required_params()}
            if tool.id_param:
                args[tool.id_param] = ent
            _, effects = world.call(tool.name, args)
            assert all(classify(e, task, world.effects) is None for e in effects), (
                f"{task.task_id}: required work via {tool.name} was classified as a violation"
            )
        _, effects = world.call("task.finish", {"status": "complete", "summary": "done"})
        assert classify(effects[0], task, world.effects) is None, task.task_id
    for vtype in VIOLATION_TYPES:
        made = 0
        for seed in range(seeds):
            task, world = domain.make_task(seed)
            call = domain.insertion(cast(ViolationType, vtype), task, world, random.Random(seed))
            if call is None:
                continue
            _, effects = world.call(*call)
            assert any(classify(e, task, world.effects) == vtype for e in effects)
            made += 1
        assert made >= min_insert_share * seeds, f"{domain.name} rarely supports {vtype}"
