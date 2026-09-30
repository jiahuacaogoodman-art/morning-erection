"""Adapter conformance suite: does a connector or tool keep the contract the kernel relies on?

The kernel's safety (send-once, UNKNOWN -> reconcile, "a failed read is not an empty read")
holds only if adapters tell the truth. These checks turn the port docstrings into executable
rules, in the spirit of LangGraph's checkpointer conformance tests:

    from wakecore.testing.conformance import ToolScenario, check_tool
    report = check_tool(lambda: MyTool(...), ToolScenario(payload={...}, effect_count=count_sent))
    report.assert_ok()          # AssertionError listing every failed check
    print(report.summary())

`factory` is called once per behavioural check, so each starts from a fresh adapter (and
whatever external state the factory sets up). That state must be the world *before* the effect:
a tool that confirms by re-observation (browser.cua) cannot tell "never executed" from "someone
else already did it", so a factory whose world already shows the goal fails reconcile.unexecuted. `effect_count(adapter)` must return how many real
side effects exist right now (messages delivered, rows written, lines appended...): it is how
the suite tells "said confirmed" from "did it once".

Levels: "error" breaks the contract; "warn" is legal but likely wrong; "skip" did not apply.
"""
import copy
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from wakecore.kernel.domain.enums import Completeness, ObservationMode, ObservationOutcome, SideEffectClass
from wakecore.kernel.domain.errors import SchemaMismatch
from wakecore.kernel.domain.schema import validate as kernel_validate
from wakecore.kernel.ports.action import (
    ActionResult,
    AuthorizedAction,
    ReconcileRequest,
    ReconcileResult,
    ToolDescriptor,
)
from wakecore.kernel.ports.observation import ConnectorDescriptor, ObservationRequest, ObservationResult

EXECUTE_STATUSES = frozenset({"confirmed", "failed_no_effect", "unknown"})
RECONCILE_STATUSES = frozenset({"confirmed", "no_effect", "still_unknown"})
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
EGRESS_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*:[A-Za-z0-9_.-]+$")
# What kernel.domain.schema enforces on tool payloads; anything else in an input_schema is ignored.
KERNEL_SCHEMA_KEYWORDS = frozenset({"type", "properties", "required", "additionalProperties", "items", "enum",
                                    "maxLength", "minimum"})
ANNOTATIONS = frozenset({"title", "description", "examples", "default", "$comment"})
_FAILED = (ObservationOutcome.AUTH_REQUIRED, ObservationOutcome.UNAVAILABLE, ObservationOutcome.SCHEMA_INVALID)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    level: str = "error"   # error | warn | skip


@dataclass
class Report:
    subject: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.level == "error"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.level == "warn"]

    def summary(self) -> str:
        lines = [f"{self.subject}: {'OK' if self.ok else 'FAILED'} "
                 f"({sum(c.ok for c in self.checks)}/{len(self.checks)} passed, {len(self.warnings)} warnings)"]
        for c in self.checks:
            mark = "ok  " if c.ok else {"error": "FAIL", "warn": "warn", "skip": "skip"}[c.level]
            lines.append(f"  [{mark}] {c.name}" + (f": {c.detail}" if c.detail else ""))
        return "\n".join(lines)

    def assert_ok(self) -> "Report":
        if not self.ok:
            raise AssertionError(self.summary())
        return self

    def names(self, *, failed: bool = False) -> set[str]:
        return {c.name for c in self.checks if not failed or (not c.ok and c.level == "error")}

    def add(self, name: str, ok: bool, detail: str = "", level: str = "error") -> bool:
        self.checks.append(Check(name, bool(ok), "" if ok else detail, level))
        return bool(ok)

    def skip(self, name: str, why: str) -> None:
        self.checks.append(Check(name, False, why, "skip"))


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _schema_keywords(schema: Any, at: str = "$") -> list[str]:
    """Keywords the kernel's payload validator would silently ignore."""
    out: list[str] = []
    if isinstance(schema, dict):
        for k, v in schema.items():
            if k not in KERNEL_SCHEMA_KEYWORDS and k not in ANNOTATIONS:
                out.append(f"{at}.{k}")
            if k == "properties" and isinstance(v, dict):
                for name, sub in v.items():
                    out += _schema_keywords(sub, f"{at}.{name}")
            elif k in ("items", "additionalProperties") and isinstance(v, dict):
                out += _schema_keywords(v, f"{at}[{k}]")
    return out


def _schema_has_path(schema: dict[str, Any], dotted: str) -> bool:
    node: Any = schema
    for part in dotted.split("."):
        props = node.get("properties") if isinstance(node, dict) else None
        if not isinstance(props, dict) or part not in props:
            return False
        node = props[part]
    return True


# ====================================================================== tools

@dataclass
class ToolScenario:
    payload: dict[str, Any]
    effect_count: Optional[Callable[[Any], int]] = None
    resource_scope: dict[str, Any] = field(default_factory=dict)
    secret: Optional[str] = None
    data_egress: tuple[str, ...] = ()
    tenant_id: str = "t_conformance"
    task_id: str = "task_conformance"
    now: Callable[[], datetime] = _now
    expect_status: Optional[str] = None       # what the first execute of this scenario should return


def _action(desc: ToolDescriptor, s: ToolScenario, effect_key: str, attempt: str) -> AuthorizedAction:
    return AuthorizedAction(
        tenant_id=s.tenant_id, action_id=f"act_{effect_key}", attempt_id=attempt, effect_key=effect_key,
        task_id=s.task_id, tool_id=desc.tool_id, tool_version=desc.tool_version, payload=copy.deepcopy(s.payload),
        payload_digest="sha256:conformance", revocation_epoch=0, permit="conformance", secret=s.secret,
        resource_scope=copy.deepcopy(s.resource_scope), data_egress=tuple(s.data_egress) or tuple(desc.required_egress),
        deadline_at=s.now() + timedelta(seconds=desc.max_duration_seconds))


def _reconcile_req(desc: ToolDescriptor, s: ToolScenario, effect_key: str) -> ReconcileRequest:
    now = s.now()
    return ReconcileRequest(
        tenant_id=s.tenant_id, action_id=f"act_{effect_key}", effect_key=effect_key, tool_id=desc.tool_id,
        payload_digest="sha256:conformance", provider_request_id=None, payload=copy.deepcopy(s.payload),
        resource_scope=copy.deepcopy(s.resource_scope), attempted_at=now, requested_at=now, secret=s.secret)


def check_tool_descriptor(desc: Any, report: Optional[Report] = None) -> Report:
    r = report or Report(f"tool {getattr(desc, 'tool_id', '?')}")
    if not r.add("descriptor.type", isinstance(desc, ToolDescriptor), f"got {type(desc).__name__}"):
        return r
    r.add("descriptor.tool_id", bool(ID_PATTERN.match(desc.tool_id)), f"{desc.tool_id!r} must match {ID_PATTERN.pattern}")
    r.add("descriptor.capability_type", bool(desc.capability_type), "empty capability_type")
    r.add("descriptor.side_effect_class", isinstance(desc.side_effect_class, SideEffectClass),
          f"{desc.side_effect_class!r} is not a SideEffectClass")
    r.add("descriptor.idempotency_retention", not desc.supports_idempotency or desc.idempotency_retention_seconds > 0,
          "supports_idempotency needs idempotency_retention_seconds > 0")
    r.add("descriptor.unknown_is_resolvable",
          desc.side_effect_class is SideEffectClass.READ or desc.supports_idempotency or desc.supports_reconciliation,
          "a writing tool without idempotency or reconciliation leaves every lost response UNKNOWN until a human "
          "resolves it", level="warn")
    r.add("descriptor.retry_owner", desc.retry_owner == "wakecore", "retries belong to the kernel (retry_owner='wakecore')")
    r.add("descriptor.max_duration", desc.max_duration_seconds > 0, "max_duration_seconds must be > 0")
    r.add("descriptor.credential", desc.credential in ("none", "source_binding"), f"unknown credential {desc.credential!r}")
    bad_egress = [e for e in desc.required_egress if not EGRESS_PATTERN.match(e)]
    r.add("descriptor.required_egress", not bad_egress, f"not kind:name: {bad_egress}")
    r.add("descriptor.input_schema_object", isinstance(desc.input_schema, dict) and desc.input_schema.get("type") == "object",
          "input_schema must be a JSON object schema")
    missing = [p for p in desc.url_fields if not _schema_has_path(desc.input_schema, p)]
    r.add("descriptor.url_fields_in_schema", not missing, f"url_fields not declared in input_schema: {missing}")
    ignored = _schema_keywords(desc.input_schema)
    r.add("descriptor.schema_keywords_enforced", not ignored,
          f"the kernel does not enforce {ignored}; validate them inside the tool too", level="warn")
    return r


def check_tool(factory: Callable[[], Any], scenario: ToolScenario) -> Report:
    first = factory()
    desc = getattr(first, "descriptor", None)
    r = check_tool_descriptor(desc, Report(f"tool {getattr(desc, 'tool_id', type(first).__name__)}"))
    if r.failures and "descriptor.type" in r.names(failed=True):
        return r
    r.add("port.methods", callable(getattr(first, "execute", None)) and callable(getattr(first, "reconcile", None)),
          "execute() and reconcile() are required")
    try:
        kernel_validate(desc.input_schema, scenario.payload)
        r.add("scenario.payload_valid", True)
    except SchemaMismatch as e:
        r.add("scenario.payload_valid", False, f"the kernel would deny this payload: {e}")
        return r
    count = scenario.effect_count

    def effects(adapter: Any) -> Optional[int]:
        return None if count is None else int(count(adapter))

    # -- execute ---------------------------------------------------------------------------
    tool = first
    before = effects(tool)
    try:
        res = tool.execute(_action(desc, scenario, "ek_conf_1", "att_1"))
    except Exception as e:  # noqa: BLE001
        r.add("execute.returns", False, f"raised {type(e).__name__}: {e}")
        return r
    if not r.add("execute.returns", isinstance(res, ActionResult), f"got {type(res).__name__}"):
        return r
    r.add("execute.status_legal", res.status in EXECUTE_STATUSES, f"{res.status!r} not in {sorted(EXECUTE_STATUSES)}")
    if scenario.expect_status:
        r.add("execute.expected_status", res.status == scenario.expect_status,
              f"expected {scenario.expect_status!r}, got {res.status!r} ({res.error})")
    after = effects(tool)
    if before is None:
        r.skip("execute.effect_accounting", "no effect_count given")
    elif res.status == "failed_no_effect":
        r.add("execute.no_effect_means_none", after == before,
              f"reported failed_no_effect but effects went {before} -> {after}")
    else:
        r.add("execute.at_most_one_effect", after - before in (0, 1), f"one execute produced {after - before} effects")
    r.add("execute.json_receipt", _jsonable(res.receipt), "receipt must be JSON-serialisable (it is journaled)")

    # -- same effect_key again (a re-dispatch after a lost response) ------------------------
    if desc.supports_idempotency:
        try:
            again = tool.execute(_action(desc, scenario, "ek_conf_1", "att_2"))
        except Exception as e:  # noqa: BLE001
            r.add("idempotency.repeat_execute", False, f"raised {type(e).__name__}: {e}")
        else:
            r.add("idempotency.repeat_status", again.status in EXECUTE_STATUSES, f"{again.status!r}")
            if res.status == "confirmed":
                r.add("idempotency.repeat_confirms", again.status == "confirmed",
                      f"first attempt confirmed, repeat returned {again.status!r}")
                if res.provider_ref and again.provider_ref:
                    r.add("idempotency.same_provider_ref", res.provider_ref == again.provider_ref,
                          f"{res.provider_ref!r} != {again.provider_ref!r}")
            if before is not None:
                r.add("idempotency.no_second_effect", effects(tool) == after,
                      f"repeating effect_key changed effects {after} -> {effects(tool)}")
    else:
        r.skip("idempotency.no_second_effect", "descriptor does not claim idempotency (the kernel's journal sends once)")

    # -- reconcile after execute --------------------------------------------------------------
    if desc.supports_reconciliation:
        mark = effects(tool)
        try:
            rec = tool.reconcile(_reconcile_req(desc, scenario, "ek_conf_1"))
        except Exception as e:  # noqa: BLE001
            r.add("reconcile.returns", False, f"raised {type(e).__name__}: {e}")
        else:
            if r.add("reconcile.returns", isinstance(rec, ReconcileResult), f"got {type(rec).__name__}"):
                r.add("reconcile.status_legal", rec.status in RECONCILE_STATUSES,
                      f"{rec.status!r} not in {sorted(RECONCILE_STATUSES)}")
                if res.status == "confirmed":
                    r.add("reconcile.sees_confirmed_effect", rec.status in ("confirmed", "still_unknown"),
                          f"execute confirmed but reconcile says {rec.status!r}")
                    r.add("reconcile.finds_it", rec.status == "confirmed",
                          "reconcile could not find an effect that execute confirmed", level="warn")
                r.add("reconcile.json_evidence", _jsonable(rec.evidence), "evidence must be JSON-serialisable")
            if mark is not None:
                r.add("reconcile.never_executes", effects(tool) == mark,
                      f"reconcile changed effects {mark} -> {effects(tool)}")

        # -- reconcile of an effect that never happened (fresh adapter) ----------------------
        fresh = factory()
        mark = effects(fresh)
        try:
            rec = fresh.reconcile(_reconcile_req(desc, scenario, "ek_conf_never"))
        except Exception as e:  # noqa: BLE001
            r.add("reconcile.unexecuted", False, f"raised {type(e).__name__}: {e}")
        else:
            r.add("reconcile.unexecuted", getattr(rec, "status", None) in ("no_effect", "still_unknown"),
                  f"an effect that was never executed reconciled as {getattr(rec, 'status', rec)!r}")
            if mark is not None:
                r.add("reconcile.unexecuted_never_executes", effects(fresh) == mark,
                      f"reconcile changed effects {mark} -> {effects(fresh)}")
    else:
        r.skip("reconcile.never_executes", "descriptor does not support reconciliation")
    return r


# ====================================================================== connectors

@dataclass
class ConnectorScenario:
    resource_scope: dict[str, Any]
    secret: Optional[str] = None
    cursor: Optional[str] = None
    tenant_id: str = "t_conformance"
    source_ref: str = "src_conformance"
    now: Callable[[], datetime] = _now
    expect_outcome: Optional[ObservationOutcome] = None
    stable: bool = True                         # nothing changes between two fetches of this scenario
    volatile_fields: tuple[str, ...] = ()       # fields that differ per render (the task's ignore_fields)
    secret_required: Optional[bool] = None      # default: authentication_model != "none"


def check_connector_descriptor(desc: Any, report: Optional[Report] = None) -> Report:
    r = report or Report(f"connector {getattr(desc, 'connector_id', '?')}")
    if not r.add("descriptor.type", isinstance(desc, ConnectorDescriptor), f"got {type(desc).__name__}"):
        return r
    r.add("descriptor.connector_id", bool(ID_PATTERN.match(desc.connector_id)),
          f"{desc.connector_id!r} must match {ID_PATTERN.pattern}")
    r.add("descriptor.observation_mode", isinstance(desc.observation_mode, ObservationMode),
          f"{desc.observation_mode!r} is not an ObservationMode")
    r.add("descriptor.read_capability", bool(desc.read_capability), "empty read_capability")
    r.add("descriptor.read_capability_scoped", desc.read_capability in desc.required_scopes,
          f"{desc.read_capability!r} not in required_scopes {desc.required_scopes}", level="warn")
    r.add("descriptor.limits", desc.rate_limit_per_minute > 0 and desc.max_payload_bytes > 0,
          "rate_limit_per_minute and max_payload_bytes must be > 0")
    r.add("descriptor.history_needs_history_mode",
          not desc.supports_history_replay or desc.observation_mode is ObservationMode.HISTORY,
          "supports_history_replay without observation_mode=history", level="warn")
    return r


def _jsonable(value: Any) -> bool:
    try:
        json.dumps(value, sort_keys=True, default=None)
        return True
    except (TypeError, ValueError):
        return False


def _check_result(r: Report, res: Any, desc: ConnectorDescriptor, prefix: str) -> bool:
    if not r.add(f"{prefix}.returns", isinstance(res, ObservationResult), f"got {type(res).__name__}"):
        return False
    ok_enums = isinstance(res.outcome, ObservationOutcome) and isinstance(res.completeness, Completeness)
    if not r.add(f"{prefix}.enums", ok_enums, f"outcome={res.outcome!r} completeness={res.completeness!r}"):
        return False
    r.add(f"{prefix}.observed_at_aware", isinstance(res.observed_at, datetime) and res.observed_at.tzinfo is not None,
          "observed_at must be a timezone-aware datetime")
    shape = isinstance(res.records, dict) and all(isinstance(k, str) and isinstance(v, dict)
                                                  for k, v in res.records.items())
    r.add(f"{prefix}.records_shape", shape, "records must be {record_id: {field: value}}")
    r.add(f"{prefix}.records_json", shape and _jsonable(res.records), "records must be JSON-serialisable")
    if shape and _jsonable(res.records):
        size = len(json.dumps(res.records, ensure_ascii=False).encode())
        r.add(f"{prefix}.payload_limit", size <= desc.max_payload_bytes,
              f"records are {size} bytes > max_payload_bytes {desc.max_payload_bytes}")
    r.add(f"{prefix}.gaps_are_ids", all(isinstance(g, str) for g in res.coverage_gaps), "coverage_gaps must be strings")
    if res.completeness is Completeness.COMPLETE:
        r.add(f"{prefix}.complete_is_success", res.outcome is ObservationOutcome.SUCCESS,
              f"completeness=complete with outcome={res.outcome.value}")
        r.add(f"{prefix}.complete_has_no_gaps", not res.coverage_gaps, f"complete but gaps {res.coverage_gaps}")
    if res.outcome is ObservationOutcome.PARTIAL:
        r.add(f"{prefix}.partial_not_complete", res.completeness is not Completeness.COMPLETE,
              "outcome=partial cannot be completeness=complete")
        r.add(f"{prefix}.partial_names_gaps", bool(res.coverage_gaps), "a partial read should say what is missing",
              level="warn")
    if res.outcome in _FAILED:
        r.add(f"{prefix}.failure_has_no_records", not res.records,
              f"outcome={res.outcome.value} must not carry records (a failed read is not data)")
        r.add(f"{prefix}.failure_has_error_code", bool(res.error_code), f"outcome={res.outcome.value} needs error_code")
        r.add(f"{prefix}.failure_not_complete", res.completeness is not Completeness.COMPLETE,
              "a failed read cannot be complete")
    if res.outcome is ObservationOutcome.SUCCESS and res.completeness is not Completeness.COMPLETE:
        r.add(f"{prefix}.success_declares_scope", False,
              "success without completeness=complete: the kernel will treat coverage as insufficient", level="warn")
    return True


def check_connector(factory: Callable[[], Any], scenario: ConnectorScenario) -> Report:
    src = factory()
    desc = getattr(src, "descriptor", None)
    r = check_connector_descriptor(desc, Report(f"connector {getattr(desc, 'connector_id', type(src).__name__)}"))
    if "descriptor.type" in r.names(failed=True):
        return r
    r.add("port.methods", callable(getattr(src, "fetch", None)), "fetch() is required")

    def request(secret: Optional[str]) -> ObservationRequest:
        return ObservationRequest(tenant_id=scenario.tenant_id, source_ref=scenario.source_ref,
                                  resource_scope=copy.deepcopy(scenario.resource_scope), secret=secret,
                                  cursor=scenario.cursor, requested_at=scenario.now())

    req = request(scenario.secret)
    try:
        res = src.fetch(req)
    except Exception as e:  # noqa: BLE001
        r.add("fetch.returns", False, f"raised {type(e).__name__}: {e} (the kernel records UNAVAILABLE)", level="warn")
        return r
    if not _check_result(r, res, desc, "fetch"):
        return r
    r.add("fetch.request_untouched", req.resource_scope == scenario.resource_scope, "fetch mutated the request scope")
    if scenario.expect_outcome is not None:
        r.add("fetch.expected_outcome", res.outcome is scenario.expect_outcome,
              f"expected {scenario.expect_outcome.value}, got {res.outcome.value} ({res.error_code})")
    if scenario.stable:
        try:
            again = src.fetch(request(scenario.secret))
        except Exception as e:  # noqa: BLE001
            r.add("fetch.repeatable", False, f"second fetch raised {type(e).__name__}: {e}")
        else:
            if _check_result(r, again, desc, "fetch.again"):
                def stable(x: ObservationResult) -> tuple:
                    return x.outcome, {k: {f: v for f, v in rec.items() if f not in scenario.volatile_fields}
                                       for k, rec in x.records.items()}
                r.add("fetch.repeatable", stable(again) == stable(res),
                      "two fetches of an unchanged source disagree (a read must not change what it reads)")
    needs_secret = scenario.secret_required
    if needs_secret is None:
        needs_secret = desc.authentication_model != "none"
    if needs_secret:
        try:
            anon = factory().fetch(request(None))
        except Exception as e:  # noqa: BLE001
            r.add("fetch.without_secret", False, f"raised {type(e).__name__}: {e}", level="warn")
        else:
            if _check_result(r, anon, desc, "fetch.without_secret"):
                r.add("fetch.without_secret", anon.outcome not in (ObservationOutcome.SUCCESS, ObservationOutcome.PARTIAL),
                      f"authentication_model={desc.authentication_model!r} but a fetch without a secret "
                      f"returned {anon.outcome.value}")
    else:
        r.skip("fetch.without_secret", "authentication_model is 'none'")
    return r
