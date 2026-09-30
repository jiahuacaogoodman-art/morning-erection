"""Reasoning port (RFC §8). Models only *propose*; the kernel admits or rejects."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Protocol

from ..domain.enums import Route


@dataclass(frozen=True, slots=True)
class ContextBundle:
    tenant_id: str
    task_id: str
    purpose: str
    event: dict[str, Any]
    evidence: tuple[dict[str, Any], ...]
    progress: dict[str, Any]
    allowed_tools: tuple[dict[str, Any], ...]
    remaining_budget: dict[str, int]
    deadline: datetime
    prompt_version: str = "v1"


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_minor_units: int = 0
    currency: str = "USD"


@dataclass(frozen=True, slots=True)
class DecisionProposal:
    route: Route
    reason_code: str
    supporting_evidence_refs: tuple[str, ...] = ()
    uncertainty_flags: tuple[str, ...] = ()
    suggested_capabilities: tuple[str, ...] = ()
    importance: str = "normal"
    usage: Usage = field(default_factory=Usage)
    provider_request_id: Optional[str] = None
    model_ref: str = "unknown"


@dataclass(frozen=True, slots=True)
class ProposedAction:
    logical_step_id: str
    tool_id: str
    capability: str
    payload: dict[str, Any]
    reason_code: str = ""
    data_egress: tuple[str, ...] = ()
    # Anything the model claims about safety/approval is ignored by policy.
    model_claims: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FollowupProposal:
    root_task_id: str
    originating_run_id: str
    reason: str
    due_at: datetime
    followup_key: str
    predicate_ref: Optional[str] = None
    evidence_refs: tuple[str, ...] = ()
    expected_action_class: str = "notify"
    resource_scope: Optional[dict[str, Any]] = None
    capabilities: tuple[str, ...] = ()
    source_ref: Optional[str] = None


@dataclass(frozen=True, slots=True)
class PlanProposal:
    steps: tuple[ProposedAction, ...]
    stop_conditions: tuple[str, ...] = ()
    followups: tuple[FollowupProposal, ...] = ()
    usage: Usage = field(default_factory=Usage)
    provider_request_id: Optional[str] = None
    model_ref: str = "unknown"


class ModelTimeout(Exception):
    """Request outcome unknown: the call may have been billed (RFC §10)."""


class ReasoningPort(Protocol):
    model_ref: str
    data_egress_target: str

    def classify(self, context: ContextBundle) -> DecisionProposal: ...

    def plan(self, context: ContextBundle) -> PlanProposal: ...
