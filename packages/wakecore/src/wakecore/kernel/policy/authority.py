"""Effective authority and action authorisation (RFC §9.1, K-02).

EffectiveAuthority = user grant ∩ task request ∩ root limits ∩ connector/account
capabilities ∩ system policy. Nothing a model or external content says can widen it.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

from ..domain.enums import AuthzOutcome, SideEffectClass
from ..domain.errors import PolicyDenied, SchemaMismatch
from ..domain.model import Grant
from ..domain.schema import validate
from ..ports.action import ToolDescriptor
from .origins import dotted, url_in_scope


@dataclass(frozen=True)
class AuthorizationDecision:
    outcome: AuthzOutcome
    reason: str


def scope_subset(requested: dict[str, Any], allowed: dict[str, Any]) -> bool:
    """Every constrained key in `allowed` must also constrain `requested` to a subset."""
    for key, allowed_value in allowed.items():
        if key not in requested:
            return False
        req = requested[key]
        if isinstance(allowed_value, list):
            req_values = req if isinstance(req, list) else [req]
            if not set(map(str, req_values)) <= set(map(str, allowed_value)):
                return False
        elif req != allowed_value:
            return False
    return True


def check_grant(grant: Optional[Grant], *, tenant_id: str, now: datetime) -> Grant:
    if grant is None or grant.tenant_id != tenant_id:
        raise PolicyDenied("grant not found for tenant")
    if grant.revoked:
        raise PolicyDenied("grant has been revoked")
    if grant.expires_at is not None and grant.expires_at <= now:
        raise PolicyDenied("grant has expired")
    return grant


def compute_effective_authority(
    *,
    grant: Grant,
    requested_capabilities: Iterable[str],
    requested_egress: Iterable[str],
    resource_scope: dict[str, Any],
    connector_capabilities: Iterable[str],
    system_capabilities: Iterable[str],
    system_egress: Iterable[str],
    root_authority: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if not scope_subset(resource_scope, grant.resource_scope):
        raise PolicyDenied("requested resource scope exceeds the grant")
    caps = set(requested_capabilities) & set(grant.capabilities) & set(system_capabilities)
    # Connector/account capabilities constrain what the source binding may be used for;
    # non-source capabilities (e.g. inbox.notify_self) are constrained by tools, not the source.
    connector = set(connector_capabilities)
    caps = {c for c in caps if not c.endswith(".read") or c in connector}
    egress = set(requested_egress) & set(grant.data_egress) & set(system_egress)
    if root_authority is not None:
        caps &= set(root_authority.get("capabilities", []))
        egress &= set(root_authority.get("data_egress", []))
        if not scope_subset(resource_scope, root_authority.get("resource_scope", {})):
            raise PolicyDenied("requested resource scope exceeds the root task")
    return {
        "grant_ref": grant.grant_ref,
        "grant_version": grant.version,
        "capabilities": sorted(caps),
        "data_egress": sorted(egress),
        "resource_scope": resource_scope,
    }


def evaluate(
    authority: dict[str, Any],
    *,
    tool: Optional[ToolDescriptor],
    capability: str,
    payload: dict[str, Any],
    data_egress: Iterable[str] = (),
    auto_approve: Iterable[str] = (),
    resource_scope: Optional[dict[str, Any]] = None,
) -> AuthorizationDecision:
    """Risk is derived from tool capability, target and parameters — never from model claims.

    `resource_scope` is the action's own scope (V0.3): it must be inside the task authority's
    scope, and every URL the tool declares in `url_fields` must be inside its origins."""
    if tool is None:
        return AuthorizationDecision(AuthzOutcome.DENY, "unknown_tool")
    if capability != tool.capability_type:
        return AuthorizationDecision(AuthzOutcome.DENY, "capability_tool_mismatch")
    if capability not in authority.get("capabilities", []):
        return AuthorizationDecision(AuthzOutcome.DENY, "capability_not_granted")
    if not set(data_egress) <= set(authority.get("data_egress", [])):
        return AuthorizationDecision(AuthzOutcome.DENY, "data_egress_not_allowed")
    if not set(tool.required_egress) <= set(data_egress):
        return AuthorizationDecision(AuthzOutcome.DENY, "tool_egress_not_declared")
    try:
        validate(tool.input_schema, payload)
    except SchemaMismatch as exc:
        return AuthorizationDecision(AuthzOutcome.DENY, f"schema:{exc}")
    scope = authority.get("resource_scope", {}) if resource_scope is None else resource_scope
    if not scope_subset(scope, authority.get("resource_scope", {})):
        return AuthorizationDecision(AuthzOutcome.DENY, "resource_scope_exceeds_authority")
    if tool.url_fields:
        origins = scope.get("origins")
        if not isinstance(origins, list) or not origins:
            return AuthorizationDecision(AuthzOutcome.DENY, "origin_scope_missing")
        for path in tool.url_fields:
            value = dotted(payload, path)
            if value is not None and not url_in_scope(value, origins):
                return AuthorizationDecision(AuthzOutcome.DENY, f"origin_outside_scope:{path}")
    if tool.side_effect_class is SideEffectClass.EXTERNAL_WRITE and capability not in set(auto_approve):
        return AuthorizationDecision(AuthzOutcome.REQUIRE_APPROVAL, "external_write")
    return AuthorizationDecision(AuthzOutcome.ALLOW, "within_authority")
