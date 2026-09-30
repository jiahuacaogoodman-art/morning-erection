"""Event admission (RFC §4.3, §7, T02, T05, T21).

Ingress only *persists* verified events and the per-task deliveries that must process
them. It never accepts control commands: nothing inside an event can activate, approve,
cancel or grant anything (K-02).
"""
import hmac
import json
from hashlib import sha256
from typing import Any, Optional

from .. import audit
from ..context import KernelContext
from ..domain.canonical import digest, short_hash
from ..domain.enums import DeliveryStatus, StepKind, TaskLifecycle
from ..domain.errors import NotFound, PolicyDenied, SchemaMismatch, Unauthenticated
from ..domain.model import Delivery, EventRecord, QuarantineRecord, Run, SourceBinding, TaskRuntime
from ..domain.taskspec import parse_ts
from ..execution.steps import add_step, create_run, run_id_for
from ..lifecycle import PAUSED_UNTIL
from ..repo import Repo

SUPPORTED_DATA_SCHEMAS = frozenset({1})
RESERVED_TYPE_PREFIX = "wakecore."


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()


def event_id_for(tenant_id: str, source_ref: str, source_event_id: str) -> str:
    return "evt_" + short_hash(tenant_id, source_ref, source_event_id)


def binding_for_ingress(repo: Repo, source_ref: str) -> SourceBinding:
    rows = repo.find(SourceBinding, {"source_ref": source_ref}, limit=2)
    if len(rows) != 1:
        raise NotFound("unknown ingress binding")
    return rows[0]


def admit(repo: Repo, ctx: KernelContext, *, source_ref: str, body: bytes,
          signature: Optional[str]) -> dict[str, Any]:
    """Returns {"status": accepted|duplicate|quarantined, ...}. Raises on authentication failure."""
    if len(body) > ctx.config.max_event_bytes:
        raise SchemaMismatch("event exceeds the maximum size")
    binding = binding_for_ingress(repo, source_ref)
    tenant = binding.tenant_id
    secret = ctx.secrets.resolve(tenant, binding.ingress_secret_ref) if binding.ingress_secret_ref else None
    if not secret or not signature or not hmac.compare_digest(sign(secret, body), signature):
        raise Unauthenticated("ingress signature invalid")
    try:
        ev = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaMismatch("event body is not JSON") from exc
    if not isinstance(ev, dict):
        raise SchemaMismatch("event must be an object")
    if ev.get("specversion") != "1.0":
        raise SchemaMismatch("unsupported CloudEvents specversion")
    for key in ("id", "source", "type"):
        if not isinstance(ev.get(key), str) or not ev[key]:
            raise SchemaMismatch(f"event.{key} is required")
    if ev["source"] != binding.source_uri:
        raise PolicyDenied("event source does not match the binding")
    if ev["type"].startswith(RESERVED_TYPE_PREFIX):
        raise PolicyDenied("reserved event type")
    data = ev.get("data", {})
    if not isinstance(data, dict):
        raise SchemaMismatch("event.data must be an object")

    now = repo.uow.db_now()
    event_id = event_id_for(tenant, source_ref, ev["id"])
    schema_version = data.get("schema_version", 1)
    if schema_version not in SUPPORTED_DATA_SCHEMAS:
        # Incompatible versions are isolated with a reason, never guessed at (RFC §14, T21).
        repo.insert_if_absent(QuarantineRecord(
            tenant_id=tenant, quarantine_id="qua_" + short_hash(event_id, "schema"), kind="event_schema",
            ref=event_id, reason=f"unsupported data schema_version {schema_version!r}",
            payload={"type": ev["type"], "id": ev["id"]}, created_at=now))
        return {"status": "quarantined", "event_id": event_id, "reason": "unsupported_schema_version"}

    payload_digest = digest({"type": ev["type"], "subject": ev.get("subject"), "data": data})
    record = EventRecord(
        tenant_id=tenant, event_id=event_id, seq=repo.uow.next_seq("events"), source_ref=source_ref,
        source_uri=binding.source_uri, source_event_id=ev["id"], specversion="1.0", type=ev["type"],
        subject=ev.get("subject"), occurred_at=parse_ts(ev["time"], "event.time") if ev.get("time") else None,
        received_at=now, payload_digest=payload_digest, data=data, verified=True, internal=False,
        ingress_principal=f"ingress:{source_ref}", causation_id=None, evidence_ref=None)
    if not repo.insert_if_absent(record):
        existing = repo.get(EventRecord, tenant_id=tenant, event_id=event_id)
        if existing is not None and existing.payload_digest == payload_digest:
            return {"status": "duplicate", "event_id": event_id}
        repo.insert_if_absent(QuarantineRecord(
            tenant_id=tenant, quarantine_id="qua_" + short_hash(event_id, payload_digest), kind="event_identity",
            ref=event_id, reason="same source event id, different content", payload={"digest": payload_digest},
            created_at=now))
        return {"status": "quarantined", "event_id": event_id, "reason": "identity_conflict", "http_status": 409}

    tasks = repo.find(TaskRuntime, {"tenant_id": tenant, "source_ref": source_ref},
                      conds=[("lifecycle", "in", [TaskLifecycle.ACTIVE, TaskLifecycle.PAUSED])],
                      order_by=("task_id",))
    for rt in tasks:
        run_id = run_id_for(tenant, rt.task_id, "evt", event_id)
        create_run(repo, rt, run_id, reason=f"event:{ev['type']}")
        run = repo.get(Run, tenant_id=tenant, run_id=run_id)
        add_step(repo, run, kind=StepKind.PROCESS_DELIVERY, logical_step_id=f"deliver:{event_id}",
                 input={"event_id": event_id},
                 available_at=PAUSED_UNTIL if rt.lifecycle is TaskLifecycle.PAUSED else now)
        repo.insert_if_absent(Delivery(tenant_id=tenant, event_id=event_id, task_id=rt.task_id,
                                       status=DeliveryStatus.PENDING, run_id=run_id, decision_ref=None, reason=None,
                                       created_at=now, updated_at=now))
    audit.record(repo, ctx.ids, tenant_id=tenant, kind="event.admitted", actor=f"ingress:{source_ref}",
                 subject_type="event", subject_id=event_id, refs={"type": ev["type"], "deliveries": len(tasks),
                                                                   "seq": record.seq})
    return {"status": "accepted", "event_id": event_id, "deliveries": len(tasks)}

