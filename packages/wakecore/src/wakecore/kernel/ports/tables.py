"""Logical table catalogue (RFC §11.1, §11.2).

Physical DDL for every adapter is generated from this single declaration so that
uniqueness guarantees cannot drift between SQLite (dev) and PostgreSQL (production).
Every key and every foreign key includes tenant_id (RFC §9.4), except the global ingress
address source_bindings.source_ref (see below).
"""
import dataclasses
import typing
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from ..domain import model as m


@dataclass(frozen=True)
class Column:
    name: str
    kind: str  # text | int | bool | ts | json
    nullable: bool


@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[str, ...]
    ref_table: str
    ref_columns: tuple[str, ...]


@dataclass(frozen=True)
class TableDef:
    name: str
    record: type
    pk: tuple[str, ...]
    unique: tuple[tuple[str, ...], ...] = ()
    indexes: tuple[tuple[str, ...], ...] = ()
    foreign_keys: tuple[ForeignKey, ...] = ()
    columns: tuple[Column, ...] = field(default=(), compare=False)


def _kind(tp: Any) -> tuple[str, bool]:
    nullable = False
    origin = typing.get_origin(tp)
    if origin is typing.Union:
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        nullable = True
        tp = args[0]
        origin = typing.get_origin(tp)
    if origin in (dict, list) or tp in (dict, list):
        return "json", nullable
    if tp is bool:
        return "bool", nullable
    if tp is int:
        return "int", nullable
    if tp is datetime:
        return "ts", nullable
    if tp is str or (isinstance(tp, type) and issubclass(tp, Enum)):
        return "text", nullable
    raise TypeError(f"unsupported column type {tp!r}")


def _columns(record: type) -> tuple[Column, ...]:
    hints = typing.get_type_hints(record)
    cols = []
    for f in dataclasses.fields(record):
        kind, nullable = _kind(hints[f.name])
        cols.append(Column(f.name, kind, nullable))
    return tuple(cols)


def _t(name: str, record: type, pk: tuple[str, ...], **kw: Any) -> TableDef:
    return TableDef(name=name, record=record, pk=pk, columns=_columns(record), **kw)


FK = ForeignKey

TABLES: dict[str, TableDef] = {
    t.name: t
    for t in [
        _t("grants", m.Grant, ("tenant_id", "grant_ref")),
        _t("task_specs", m.TaskSpecRecord, ("tenant_id", "task_id", "spec_version")),
        _t("task_runtimes", m.TaskRuntime, ("tenant_id", "task_id"),
           indexes=(("tenant_id", "root_task_id"), ("tenant_id", "source_ref", "lifecycle")),
           foreign_keys=(FK(("tenant_id", "task_id", "active_spec_version"), "task_specs",
                            ("tenant_id", "task_id", "spec_version")),)),
        # The one deliberate cross-tenant key: ingress addresses a binding by source_ref alone
        # (POST /v1/ingress/{binding_id}), so the database, not only setup code, keeps it unique.
        _t("source_bindings", m.SourceBinding, ("tenant_id", "source_ref"), unique=(("source_ref",),)),
        _t("source_checkpoints", m.SourceCheckpoint, ("tenant_id", "source_ref", "scope_key"),
           foreign_keys=(FK(("tenant_id", "source_ref"), "source_bindings", ("tenant_id", "source_ref")),)),
        _t("triggers", m.TriggerRecord, ("tenant_id", "trigger_id"),
           unique=(("tenant_id", "root_task_id", "followup_key"),),
           indexes=(("status", "due_at"), ("tenant_id", "task_id"))),
        _t("trigger_occurrences", m.TriggerOccurrence,
           ("tenant_id", "trigger_id", "generation", "scheduled_for"), indexes=(("tenant_id", "run_id"),),
           foreign_keys=(FK(("tenant_id", "trigger_id"), "triggers", ("tenant_id", "trigger_id")),)),
        _t("observations", m.ObservationRecord, ("tenant_id", "observation_id"),
           indexes=(("tenant_id", "task_id", "observed_at"),)),
        _t("evidence", m.Evidence, ("tenant_id", "evidence_ref")),
        _t("events", m.EventRecord, ("tenant_id", "event_id"),
           unique=(("tenant_id", "source_ref", "source_event_id"),),
           indexes=(("tenant_id", "seq"),)),
        _t("deliveries", m.Delivery, ("tenant_id", "event_id", "task_id"),
           indexes=(("tenant_id", "task_id", "status"),),
           foreign_keys=(FK(("tenant_id", "event_id"), "events", ("tenant_id", "event_id")),)),
        _t("decisions", m.DecisionRecord, ("tenant_id", "decision_id"),
           indexes=(("tenant_id", "task_id"),)),
        _t("runs", m.Run, ("tenant_id", "run_id"), indexes=(("tenant_id", "task_id", "status"),)),
        _t("steps", m.Step, ("tenant_id", "step_id"),
           unique=(("tenant_id", "run_id", "logical_step_id"),),
           indexes=(("status", "available_at"), ("status", "lease_until")),
           foreign_keys=(FK(("tenant_id", "run_id"), "runs", ("tenant_id", "run_id")),)),
        _t("wait_records", m.WaitRecord, ("tenant_id", "wait_id"), indexes=(("status", "due_at"),)),
        _t("execution_slots", m.ExecutionSlot, ("tenant_id", "task_id")),
        _t("actions", m.ActionRecord, ("tenant_id", "action_id"),
           unique=(("tenant_id", "task_id", "effect_key"),),
           indexes=(("tenant_id", "root_task_id", "status"), ("status",))),
        _t("action_attempts", m.ActionAttempt, ("tenant_id", "attempt_id"),
           unique=(("tenant_id", "provider_ref", "provider_request_id"),),
           indexes=(("tenant_id", "action_id"),),
           foreign_keys=(FK(("tenant_id", "action_id"), "actions", ("tenant_id", "action_id")),)),
        _t("approvals", m.Approval, ("tenant_id", "approval_id"),
           unique=(("tenant_id", "action_id", "approval_revision"),),
           foreign_keys=(FK(("tenant_id", "action_id"), "actions", ("tenant_id", "action_id")),)),
        _t("outbox", m.OutboxEntry, ("tenant_id", "outbox_id"),
           unique=(("tenant_id", "dedupe_key"),), indexes=(("status", "available_at"),)),
        _t("budget_accounts", m.BudgetAccount, ("tenant_id", "account_key")),
        _t("budget_reservations", m.BudgetReservation, ("tenant_id", "reservation_id", "account_key"),
           foreign_keys=(FK(("tenant_id", "account_key"), "budget_accounts", ("tenant_id", "account_key")),)),
        _t("model_calls", m.ModelCall, ("tenant_id", "call_id"),
           indexes=(("tenant_id", "run_id", "logical_step_id"),)),
        _t("audit_entries", m.AuditEntry, ("tenant_id", "audit_id"), indexes=(("tenant_id", "task_id", "seq"),)),
        _t("idempotency_records", m.IdempotencyRecord, ("tenant_id", "principal", "idem_key")),
        _t("inbox_messages", m.InboxMessage, ("tenant_id", "message_id"),
           unique=(("tenant_id", "effect_key"),)),
        _t("quarantine", m.QuarantineRecord, ("tenant_id", "quarantine_id")),
    ]
}

RECORD_TABLE: dict[type, str] = {t.record: t.name for t in TABLES.values()}


def table_for(record_cls: type) -> TableDef:
    return TABLES[RECORD_TABLE[record_cls]]


def column_kinds(table: str) -> dict[str, str]:
    return {c.name: c.kind for c in TABLES[table].columns}


def optional_kind(table: str, column: str) -> Optional[str]:
    return column_kinds(table).get(column)
