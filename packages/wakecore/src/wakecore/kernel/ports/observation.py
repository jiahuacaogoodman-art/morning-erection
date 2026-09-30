"""Source connector port (RFC §13.1). Connectors return typed results; they never mutate tasks."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Protocol

from ..domain.enums import Completeness, ObservationMode, ObservationOutcome


@dataclass(frozen=True, slots=True)
class ConnectorDescriptor:
    connector_id: str
    version: str
    supported_resource_types: tuple[str, ...]
    observation_mode: ObservationMode
    supports_history_replay: bool
    supports_entity_revision: bool
    authentication_model: str
    required_scopes: tuple[str, ...]
    rate_limit_per_minute: int
    max_payload_bytes: int
    snapshot_completeness_contract: str
    read_capability: str


@dataclass(frozen=True, slots=True)
class ObservationRequest:
    tenant_id: str
    source_ref: str
    resource_scope: dict[str, Any]
    secret: Optional[str]
    cursor: Optional[str]
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class ObservationResult:
    """RFC §4.2: success, partial and failure are different facts (K-04)."""

    outcome: ObservationOutcome
    completeness: Completeness
    scope: dict[str, Any]
    records: dict[str, dict[str, Any]]
    observed_at: datetime
    source_revision: Optional[str] = None
    cursor: Optional[str] = None
    coverage_gaps: tuple[str, ...] = ()
    history_coverage: Optional[dict[str, Any]] = None
    error_code: Optional[str] = None
    retry_after_seconds: Optional[int] = None
    raw_excerpt: dict[str, Any] = field(default_factory=dict)


class ObservationPort(Protocol):
    descriptor: ConnectorDescriptor

    def fetch(self, request: ObservationRequest) -> ObservationResult: ...
