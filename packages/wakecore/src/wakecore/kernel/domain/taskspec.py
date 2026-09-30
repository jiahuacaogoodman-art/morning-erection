"""TaskSpec value object (RFC §4.1). Strict parser: unknown fields are rejected."""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .canonical import digest
from .enums import CatchupPolicy, TriggerKind
from .errors import SchemaMismatch

SUPPORTED_SCHEMA_VERSIONS = frozenset({1})
FIRST_SNAPSHOT_POLICIES = frozenset({"notify_existing_results", "baseline_only"})
COMPLETENESS_POLICIES = frozenset({"full_target_scope", "declared_scope"})


def _require(d: dict[str, Any], key: str, where: str) -> Any:
    if key not in d:
        raise SchemaMismatch(f"{where}.{key} is required")
    return d[key]


def _no_unknown(d: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(d) - allowed
    if unknown:
        raise SchemaMismatch(f"unknown fields in {where}: {sorted(unknown)}")


def parse_ts(value: Any, where: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SchemaMismatch(f"{where} is not an ISO-8601 timestamp") from exc
    else:
        raise SchemaMismatch(f"{where} must be a timestamp")
    if dt.tzinfo is None:
        raise SchemaMismatch(f"{where} must carry a timezone offset")
    return dt.astimezone(timezone.utc)


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SchemaMismatch(f"{where} must be a list of strings")
    return tuple(value)


def _pos_int(value: Any, where: str, *, allow_zero: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < (0 if allow_zero else 1):
        raise SchemaMismatch(f"{where} must be a {'non-negative' if allow_zero else 'positive'} integer")
    return value


@dataclass(frozen=True, slots=True)
class SourceScope:
    binding_ref: str
    resource_scope: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TriggerConfig:
    kind: TriggerKind
    every_seconds: Optional[int]
    timezone: str
    catchup_policy: CatchupPolicy
    due_at: Optional[datetime] = None


@dataclass(frozen=True, slots=True)
class ObservationPolicy:
    completeness_required: str
    first_snapshot: str
    ignore_fields: tuple[str, ...]
    target_key: str = "course_ids"


@dataclass(frozen=True, slots=True)
class CompletionSpec:
    evaluator: str
    evaluator_version: int
    required_outputs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuthorityRequest:
    grant_ref: str
    read_capabilities: tuple[str, ...]
    write_capabilities: tuple[str, ...]
    notify_capabilities: tuple[str, ...]
    model_data_egress: tuple[str, ...]

    def requested_capabilities(self) -> frozenset[str]:
        return frozenset(self.read_capabilities + self.write_capabilities + self.notify_capabilities)


@dataclass(frozen=True, slots=True)
class Limits:
    max_steps_per_run: int = 8
    max_run_seconds: int = 120
    max_model_attempts_per_day: int = 4
    min_probe_interval_seconds: int = 300
    max_followup_count: int = 2
    max_followup_depth: int = 1


@dataclass(frozen=True, slots=True)
class FollowupPolicy:
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class DecisionSpec:
    """V0.3: which decision profile interprets observations. Absent == grades.v1 (and absent
    specs keep their original digest)."""
    profile: str
    params: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TaskSpec:
    schema_version: int
    tenant_id: str
    task_id: str
    root_task_id: str
    spec_version: int
    purpose: str
    source: SourceScope
    trigger: TriggerConfig
    observation: ObservationPolicy
    completion: CompletionSpec
    authority: AuthorityRequest
    limits: Limits
    followup: FollowupPolicy
    expires_at: datetime
    parent_task_id: Optional[str] = None
    depth: int = 0
    accounting_timezone: str = field(default="UTC")
    decision: Optional[DecisionSpec] = None

    @property
    def decision_profile(self) -> str:
        return self.decision.profile if self.decision else "grades.v1"

    @property
    def decision_params(self) -> dict[str, Any]:
        return dict(self.decision.params) if self.decision else {}

    @property
    def target_resources(self) -> tuple[str, ...]:
        value = self.source.resource_scope.get(self.observation.target_key, [])
        return tuple(value) if isinstance(value, list) else ()

    def to_dict(self) -> dict[str, Any]:
        return spec_to_dict(self)

    def digest(self) -> str:
        return digest(self.to_dict())


_TOP = {
    "schema_version", "task_id", "root_task_id", "tenant_id", "spec_version", "purpose", "source",
    "trigger", "observation", "completion", "authority", "limits", "followup", "expires_at",
    "parent_task_id", "depth", "accounting_timezone", "decision",
}


def parse_task_spec(raw: dict[str, Any], *, tenant_id: Optional[str] = None) -> TaskSpec:
    """Parse and validate. `tenant_id` from the authenticated principal wins over the body."""
    if not isinstance(raw, dict):
        raise SchemaMismatch("task spec must be an object")
    _no_unknown(raw, _TOP, "spec")
    version = _require(raw, "schema_version", "spec")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise SchemaMismatch(f"unsupported TaskSpec schema_version {version!r}")
    body_tenant = raw.get("tenant_id")
    if tenant_id is not None and body_tenant not in (None, tenant_id):
        raise SchemaMismatch("tenant_id in body does not match the authenticated tenant")
    tenant = tenant_id or body_tenant
    if not tenant:
        raise SchemaMismatch("tenant_id is required")
    task_id = _require(raw, "task_id", "spec")
    if not isinstance(task_id, str) or not task_id:
        raise SchemaMismatch("task_id must be a non-empty string")

    src = _require(raw, "source", "spec")
    _no_unknown(src, {"binding_ref", "resource_scope"}, "source")
    source = SourceScope(binding_ref=_require(src, "binding_ref", "source"),
                         resource_scope=dict(src.get("resource_scope", {})))

    trg = _require(raw, "trigger", "spec")
    _no_unknown(trg, {"kind", "every_seconds", "timezone", "catchup_policy", "due_at"}, "trigger")
    try:
        kind = TriggerKind(_require(trg, "kind", "trigger"))
        catchup = CatchupPolicy(trg.get("catchup_policy", "coalesce_latest"))
    except ValueError as exc:
        raise SchemaMismatch(str(exc)) from exc
    every = trg.get("every_seconds")
    due_at = parse_ts(trg["due_at"], "trigger.due_at") if trg.get("due_at") else None
    if kind is TriggerKind.INTERVAL:
        every = _pos_int(every, "trigger.every_seconds")
    elif due_at is None:
        raise SchemaMismatch("trigger.due_at is required for once triggers")
    trigger = TriggerConfig(kind=kind, every_seconds=every, timezone=trg.get("timezone", "UTC"),
                            catchup_policy=catchup, due_at=due_at)

    obs = raw.get("observation", {})
    _no_unknown(obs, {"completeness_required", "first_snapshot", "ignore_fields", "target_key"}, "observation")
    observation = ObservationPolicy(
        completeness_required=obs.get("completeness_required", "full_target_scope"),
        first_snapshot=obs.get("first_snapshot", "baseline_only"),
        ignore_fields=_str_list(obs.get("ignore_fields", []), "observation.ignore_fields"),
        target_key=obs.get("target_key", "course_ids"),
    )
    if observation.completeness_required not in COMPLETENESS_POLICIES:
        raise SchemaMismatch("observation.completeness_required is not supported")
    if observation.first_snapshot not in FIRST_SNAPSHOT_POLICIES:
        raise SchemaMismatch("observation.first_snapshot is not supported")

    comp = _require(raw, "completion", "spec")
    _no_unknown(comp, {"evaluator", "evaluator_version", "required_outputs"}, "completion")
    completion = CompletionSpec(
        evaluator=_require(comp, "evaluator", "completion"),
        evaluator_version=_pos_int(comp.get("evaluator_version", 1), "completion.evaluator_version"),
        required_outputs=_str_list(comp.get("required_outputs", []), "completion.required_outputs"),
    )

    auth = _require(raw, "authority", "spec")
    _no_unknown(auth, {"grant_ref", "read_capabilities", "write_capabilities", "notify_capabilities",
                       "model_data_egress"}, "authority")
    authority = AuthorityRequest(
        grant_ref=_require(auth, "grant_ref", "authority"),
        read_capabilities=_str_list(auth.get("read_capabilities", []), "authority.read_capabilities"),
        write_capabilities=_str_list(auth.get("write_capabilities", []), "authority.write_capabilities"),
        notify_capabilities=_str_list(auth.get("notify_capabilities", []), "authority.notify_capabilities"),
        model_data_egress=_str_list(auth.get("model_data_egress", []), "authority.model_data_egress"),
    )

    lim = raw.get("limits", {})
    _no_unknown(lim, set(Limits.__dataclass_fields__), "limits")
    defaults = Limits()
    limits = Limits(**{k: _pos_int(lim.get(k, getattr(defaults, k)), f"limits.{k}", allow_zero=True)
                       for k in Limits.__dataclass_fields__})
    if trigger.every_seconds is not None and trigger.every_seconds < limits.min_probe_interval_seconds:
        raise SchemaMismatch("trigger.every_seconds is below limits.min_probe_interval_seconds")

    fol = raw.get("followup", {})
    _no_unknown(fol, {"enabled"}, "followup")
    followup = FollowupPolicy(enabled=bool(fol.get("enabled", False)))

    decision = None
    if "decision" in raw:
        dec = raw["decision"]
        if not isinstance(dec, dict):
            raise SchemaMismatch("decision must be an object")
        _no_unknown(dec, {"profile", "params"}, "decision")
        profile = _require(dec, "profile", "decision")
        params = dec.get("params", {})
        if not isinstance(profile, str) or not profile or not isinstance(params, dict):
            raise SchemaMismatch("decision.profile must be a string and decision.params an object")
        decision = DecisionSpec(profile=profile, params=dict(params))

    depth = raw.get("depth", 0)
    return TaskSpec(
        schema_version=version,
        tenant_id=tenant,
        task_id=task_id,
        root_task_id=raw.get("root_task_id") or task_id,
        spec_version=_pos_int(raw.get("spec_version", 1), "spec_version"),
        purpose=str(raw.get("purpose", "")),
        source=source,
        trigger=trigger,
        observation=observation,
        completion=completion,
        authority=authority,
        limits=limits,
        followup=followup,
        expires_at=parse_ts(_require(raw, "expires_at", "spec"), "expires_at"),
        parent_task_id=raw.get("parent_task_id"),
        depth=_pos_int(depth, "depth", allow_zero=True),
        accounting_timezone=raw.get("accounting_timezone", trigger.timezone),
        decision=decision,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items() if v is not None}
    return value


def spec_to_dict(spec: TaskSpec) -> dict[str, Any]:
    return _jsonable(asdict(spec))
