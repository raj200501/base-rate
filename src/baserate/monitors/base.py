"""The monitor interface.

A monitor maps the monitor-visible view of a trace (``Trace.monitor_view``) to
a suspicion score in [0, 1], higher meaning more suspicious, and optionally to
the index of the first action it flags. Every monitor family (rule monitors,
model monitors, ensembles, cascades) implements :class:`Monitor`, so the
estimators in ``baserate.estimate`` treat them alike.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from baserate.traces.schema import Trace


@dataclass(frozen=True)
class Score:
    """A monitor's output for one trace."""

    trace_id: str
    score: float
    flagged_step: int | None = None
    cost: float = 0.0
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"score must be in [0, 1], got {self.score}")
        if self.cost < 0:
            raise ValueError("cost must be non-negative")


@runtime_checkable
class Monitor(Protocol):
    """Anything that scores traces.

    Attributes:
        name: Stable identifier used in artifacts.
        family: One of ``rule``, ``model``, ``ensemble``, ``cascade``, ``judge``.
    """

    name: str
    family: str

    def score(self, trace: Trace) -> Score:
        """Score one trace using only ``trace.monitor_view()``."""
        ...


def score_all(monitor: Monitor, traces: Iterable[Trace]) -> list[Score]:
    """Score every trace with ``monitor``, preserving order."""
    return [monitor.score(t) for t in traces]
