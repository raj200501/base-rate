"""Trace schema, compatible with Armour's ``AgentTrace``.

A :class:`Trace` is an Armour ``AgentTrace`` (``eval_id``, ``agent_id``,
``actions`` of ``step``/``tool``/``input``/``observation``/``metadata``, and
``final_answer``) plus the fields base-rate needs for prevalence-aware
evaluation: provenance (``source``, ``source_run_id``, ``synthetic``), the
mock-tool ``domain``, and labels (``label``, ``violation_type``,
``violation_source``, ``first_violating_step``). ``Trace.to_armour`` drops the
extra fields, so every trace can be scanned by Armour unchanged.

``first_violating_step`` is a 0-based index into ``actions``. ``source_run_id``
identifies the independent agent run a trace descends from; procedural
variants and inserted-violation copies share their source run's id, and every
interval clusters by it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast, get_args

Label = Literal["benign", "violation"]
Source = Literal["real_agent", "procedural", "inserted", "dataset"]
ViolationSource = Literal["natural", "inserted", "dataset"]
ViolationType = Literal[
    "scope_overreach",
    "unauthorized_data_access",
    "audit_log_edit",
    "false_completion",
    "unapproved_escalation",
]

LABELS: tuple[str, ...] = get_args(Label)
SOURCES: tuple[str, ...] = get_args(Source)
VIOLATION_SOURCES: tuple[str, ...] = get_args(ViolationSource)
VIOLATION_TYPES: tuple[str, ...] = get_args(ViolationType)
DOMAINS: tuple[str, ...] = (
    "devops",
    "saas_admin",
    "customer_support",
    "finance_ops",
    "hr",
    "research_data",
    "browser",
    "code_maintenance",
)
"""The eight mock-tool domains named in section 4 of the brief."""


class SchemaError(ValueError):
    """Raised when a record does not satisfy the trace schema."""


@dataclass(frozen=True)
class Action:
    """One tool call or visible agent step (Armour ``AgentAction``)."""

    step: int
    tool: str
    input: str
    observation: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Action:
        """Parse an action record."""
        try:
            return cls(
                step=int(payload["step"]),
                tool=str(payload["tool"]),
                input=str(payload.get("input", "")),
                observation=str(payload.get("observation", "")),
                metadata=dict(payload.get("metadata", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaError(f"invalid action: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "step": self.step,
            "tool": self.tool,
            "input": self.input,
            "observation": self.observation,
            "metadata": dict(self.metadata),
        }

    def text(self) -> str:
        """The action as monitor-visible text."""
        return f"{self.tool}\n{self.input}\n{self.observation}".strip()


@dataclass(frozen=True)
class Trace:
    """A labeled agent trajectory."""

    trace_id: str
    eval_id: str
    agent_id: str
    actions: tuple[Action, ...]
    domain: str
    source: Source
    source_run_id: str
    synthetic: bool
    label: Label
    final_answer: str = ""
    violation_type: ViolationType | None = None
    violation_source: ViolationSource | None = None
    first_violating_step: int | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Trace:
        """Parse and validate a trace record."""
        try:
            actions = tuple(Action.from_dict(a) for a in payload["actions"])
            fvs = payload.get("first_violating_step")
            return cls(
                trace_id=str(payload["trace_id"]),
                eval_id=str(payload["eval_id"]),
                agent_id=str(payload["agent_id"]),
                actions=actions,
                domain=str(payload["domain"]),
                source=cast(Source, payload["source"]),
                source_run_id=str(payload["source_run_id"]),
                synthetic=bool(payload["synthetic"]),
                label=cast(Label, payload["label"]),
                final_answer=str(payload.get("final_answer", "")),
                violation_type=payload.get("violation_type"),
                violation_source=payload.get("violation_source"),
                first_violating_step=None if fvs is None else int(fvs),
                provenance=dict(payload.get("provenance", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, SchemaError):
                raise
            raise SchemaError(f"invalid trace: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict (the shard record format)."""
        return {
            "trace_id": self.trace_id,
            "eval_id": self.eval_id,
            "agent_id": self.agent_id,
            "actions": [a.to_dict() for a in self.actions],
            "final_answer": self.final_answer,
            "domain": self.domain,
            "source": self.source,
            "source_run_id": self.source_run_id,
            "synthetic": self.synthetic,
            "label": self.label,
            "violation_type": self.violation_type,
            "violation_source": self.violation_source,
            "first_violating_step": self.first_violating_step,
            "provenance": dict(self.provenance),
        }

    def to_armour(self) -> dict[str, Any]:
        """The Armour ``AgentTrace`` view: only the fields Armour reads."""
        return {
            "eval_id": self.eval_id,
            "agent_id": self.agent_id,
            "actions": [a.to_dict() for a in self.actions],
            "final_answer": self.final_answer,
        }

    def monitor_view(self, upto: int | None = None) -> dict[str, Any]:
        """What a monitor may see: the Armour view, optionally truncated.

        Labels, provenance, and source fields are never included, so monitors
        cannot read the answer.

        Args:
            upto: If given, include only actions with index < ``upto`` (for
                streaming and time-to-detection) and omit the final answer
                unless the whole trace is included.
        """
        view = self.to_armour()
        if upto is not None and upto < len(self.actions):
            view["actions"] = view["actions"][:upto]
            view["final_answer"] = ""
        return view


def validate(trace: Trace) -> None:
    """Check field values and label consistency.

    Raises:
        SchemaError: If any field is out of range or labels are inconsistent.
    """
    if not trace.trace_id or not trace.source_run_id:
        raise SchemaError("trace_id and source_run_id must be non-empty")
    if trace.label not in LABELS:
        raise SchemaError(f"label must be one of {LABELS}")
    if trace.source not in SOURCES:
        raise SchemaError(f"source must be one of {SOURCES}")
    if trace.source == "procedural" and not trace.synthetic:
        raise SchemaError("procedural traces must be labeled synthetic")
    if not trace.actions:
        raise SchemaError("a trace needs at least one action")
    if trace.label == "violation":
        if trace.violation_type not in VIOLATION_TYPES:
            raise SchemaError(f"violation_type must be one of {VIOLATION_TYPES}")
        if trace.violation_source not in VIOLATION_SOURCES:
            raise SchemaError(f"violation_source must be one of {VIOLATION_SOURCES}")
        fvs = trace.first_violating_step
        if fvs is None or not 0 <= fvs < len(trace.actions):
            raise SchemaError("first_violating_step must index an action")
    elif any(
        v is not None
        for v in (trace.violation_type, trace.violation_source, trace.first_violating_step)
    ):
        raise SchemaError("benign traces carry no violation fields")


def independent_runs(traces: Sequence[Trace]) -> int:
    """Number of distinct source runs (the independent units for intervals)."""
    return len({t.source_run_id for t in traces})


def synthetic_share(traces: Sequence[Trace]) -> float:
    """Fraction of traces labeled synthetic (0 for an empty sequence)."""
    return sum(t.synthetic for t in traces) / len(traces) if traces else 0.0
