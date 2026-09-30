"""Operator/user setup commands: grants and source bindings (RFC §9.1, §4.3).

Grants are only ever created by an authenticated control command, never from external
content (K-02). Changing a grant bumps its version, which invalidates approvals bound to
the old version.
"""
from datetime import datetime
from typing import Any, Optional

from .. import audit
from ..context import KernelContext
from ..domain.enums import SourceHealth
from ..domain.errors import IdentityConflict, NotFound, PolicyDenied, SchemaMismatch
from ..domain.model import Grant, SourceBinding
from ..repo import Repo


def register_grant(repo: Repo, ctx: KernelContext, *, tenant_id: str, principal: str, grant_ref: str,
                   capabilities: list[str], data_egress: list[str], resource_scope: dict[str, Any],
                   expires_at: Optional[datetime] = None) -> Grant:
    now = repo.uow.db_now()
    existing = repo.get(Grant, for_update=True, tenant_id=tenant_id, grant_ref=grant_ref)
    if existing is not None:
        if existing.principal != principal:
            raise PolicyDenied("grant belongs to another principal")
        grant = repo.change(existing, expect={"version": existing.version}, version=existing.version + 1,
                            capabilities=sorted(set(capabilities)), data_egress=sorted(set(data_egress)),
                            resource_scope=resource_scope, expires_at=expires_at, revoked=False)
    else:
        grant = Grant(tenant_id=tenant_id, grant_ref=grant_ref, principal=principal, version=1,
                      capabilities=sorted(set(capabilities)), data_egress=sorted(set(data_egress)),
                      resource_scope=resource_scope, expires_at=expires_at, revoked=False, created_at=now)
        repo.insert(grant)
    audit.record(repo, ctx.ids, tenant_id=tenant_id, kind="grant.registered", actor=principal, subject_type="grant",
                 subject_id=grant_ref, refs={"version": grant.version, "capabilities": grant.capabilities,
                                             "data_egress": grant.data_egress})
    return grant


def revoke_grant(repo: Repo, ctx: KernelContext, *, tenant_id: str, principal: str, grant_ref: str) -> Grant:
    grant = repo.get(Grant, for_update=True, tenant_id=tenant_id, grant_ref=grant_ref)
    if grant is None:
        raise NotFound(f"grant {grant_ref}")
    if grant.principal != principal:
        raise PolicyDenied("grant belongs to another principal")
    grant = repo.change(grant, expect={"version": grant.version}, revoked=True, version=grant.version + 1)
    audit.record(repo, ctx.ids, tenant_id=tenant_id, kind="grant.revoked", actor=principal, subject_type="grant",
                 subject_id=grant_ref, refs={"version": grant.version})
    return grant


def register_source_binding(repo: Repo, ctx: KernelContext, *, tenant_id: str, owner: str, source_ref: str,
                            connector_id: str, source_uri: str, resource_scope: dict[str, Any],
                            capabilities: list[str], secret_ref: Optional[str] = None,
                            ingress_secret_ref: Optional[str] = None) -> SourceBinding:
    if not source_ref or "/" in source_ref:
        raise SchemaMismatch("source_ref must be a non-empty path segment")
    if connector_id not in ctx.connectors:
        raise NotFound(f"connector {connector_id} is not installed")
    # Ingress addresses a binding by source_ref alone, so it must be globally unique.
    clash = repo.find(SourceBinding, {"source_ref": source_ref}, limit=1)
    if clash:
        if clash[0].tenant_id != tenant_id or clash[0].owner != owner:
            raise IdentityConflict("source_ref already registered")
        return clash[0]
    now = repo.uow.db_now()
    binding = SourceBinding(
        tenant_id=tenant_id, source_ref=source_ref, owner=owner, connector_id=connector_id, source_uri=source_uri,
        resource_scope=resource_scope, secret_ref=secret_ref, ingress_secret_ref=ingress_secret_ref,
        capabilities=sorted(set(capabilities)), health=SourceHealth.HEALTHY, health_reason=None,
        last_trusted_at=None, last_attempt_at=None, created_at=now, updated_at=now)
    repo.insert(binding)
    audit.record(repo, ctx.ids, tenant_id=tenant_id, kind="source.registered", actor=owner, subject_type="source",
                 subject_id=source_ref, refs={"connector_id": connector_id})
    return binding


def mark_source_reauthorised(repo: Repo, ctx: KernelContext, *, tenant_id: str, owner: str,
                             source_ref: str) -> SourceBinding:
    """The user re-authenticated; the next probe decides whether the source is really healthy."""
    b = repo.get(SourceBinding, for_update=True, tenant_id=tenant_id, source_ref=source_ref)
    if b is None:
        raise NotFound(f"source {source_ref}")
    if b.owner != owner:
        raise PolicyDenied("source belongs to another principal")
    now = repo.uow.db_now()
    b = repo.change(b, health=SourceHealth.HEALTHY, health_reason="reauthorised", updated_at=now)
    audit.record(repo, ctx.ids, tenant_id=tenant_id, kind="source.reauthorised", actor=owner,
                 subject_type="source", subject_id=source_ref)
    return b
