"""Kernel wiring: the set of ports and configuration every service receives."""
from dataclasses import dataclass, field
from typing import Optional

from .ports.action import ActionPort
from .ports.clock import Clock, IdGenerator
from .ports.faults import NoFaults
from .ports.observation import ObservationPort
from .ports.reasoning import ReasoningPort
from .ports.secrets import SecretsPort
from .ports.store import StateStore


@dataclass(frozen=True)
class KernelConfig:
    lease_seconds: int = 30
    dispatch_lease_seconds: int = 60
    max_step_attempts: int = 5
    retry_base_seconds: int = 5
    retry_max_seconds: int = 900
    scheduler_batch: int = 50
    recovery_batch: int = 50
    claim_candidates: int = 20
    approval_ttl_seconds: int = 24 * 3600
    max_event_bytes: int = 256 * 1024
    user_model_attempts_per_day: int = 50
    policy_version: str = "policy-v1"
    rules_version: str = "rules-v1"
    reducer_version: str = "reducer-v1"
    notify_tool_id: str = "inbox.notify"
    notify_capability: str = "inbox.notify_self"


@dataclass(frozen=True)
class SystemPolicy:
    """Operator-level safety policy: the last term of EffectiveAuthority (RFC §9.1)."""

    allowed_capabilities: frozenset[str] = frozenset(
        {"grades.read", "inbox.notify_self", "email.send", "email.read"}
    )
    allowed_data_egress: frozenset[str] = frozenset({"model:offline-scripted"})
    auto_approve_capabilities: frozenset[str] = frozenset()


class _NoSecrets:
    def resolve(self, tenant_id: str, secret_ref: str) -> Optional[str]:
        return None


@dataclass
class KernelContext:
    store: StateStore
    clock: Clock
    ids: IdGenerator
    connectors: dict[str, ObservationPort] = field(default_factory=dict)
    tools: dict[str, ActionPort] = field(default_factory=dict)
    reasoning: Optional[ReasoningPort] = None
    secrets: SecretsPort = field(default_factory=_NoSecrets)
    system_policy: SystemPolicy = field(default_factory=SystemPolicy)
    faults: object = field(default_factory=NoFaults)
    config: KernelConfig = field(default_factory=KernelConfig)
