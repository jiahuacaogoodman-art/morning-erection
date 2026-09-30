"""Example WakeCore plugin (copy this to start your own).

Connector `example.json_file` watches a JSON file of records, e.g. an export another program
rewrites from time to time:

    {"records": {"A-1": {"status": "open"}, "A-2": {"status": "closed"}}}

    resource_scope = {"path": "orders.json", "record_ids": ["A-1", "A-2"]}

Tool `example.journal` appends one line per effect to a journal file. It is idempotent by
effect_key (the line carries the key, a repeated execute finds it and does nothing) and
reconcilable (reconcile only reads the file). Both only touch files under the operator's
`root` directory (plugin config), so a task cannot point them anywhere else.

Things every adapter must get right, and that `wakecore.testing.conformance` checks:
  * a failed read is an outcome (AUTH_REQUIRED / UNAVAILABLE / SCHEMA_INVALID ...), not a
    successful read of nothing; a target you could not read is a coverage gap
  * execute returns confirmed / failed_no_effect / unknown and never repeats an effect for the
    same effect_key; reconcile observes and never executes
"""
import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from wakecore.kernel.domain.enums import Completeness, ObservationMode, ObservationOutcome, SideEffectClass
from wakecore.kernel.ports.action import (
    ActionResult,
    AuthorizedAction,
    ReconcileRequest,
    ReconcileResult,
    ToolDescriptor,
)
from wakecore.kernel.ports.observation import ConnectorDescriptor, ObservationRequest, ObservationResult
from wakecore.plugins import PluginContext


def _inside(root: Path, rel: Any) -> Optional[Path]:
    if not isinstance(rel, str) or not rel or os.path.isabs(rel):
        return None
    p = (root / rel).resolve()
    return p if p.is_relative_to(root) else None


class JsonFileSource:
    descriptor = ConnectorDescriptor(
        connector_id="example.json_file", version="0.1.0", supported_resource_types=("json_record",),
        observation_mode=ObservationMode.SNAPSHOT, supports_history_replay=False, supports_entity_revision=False,
        authentication_model="none", required_scopes=("files.read",), rate_limit_per_minute=60,
        max_payload_bytes=1024 * 1024, snapshot_completeness_contract="full_target_scope_when_success",
        read_capability="files.read")

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    def fetch(self, request: ObservationRequest) -> ObservationResult:
        now, scope = request.requested_at, request.resource_scope
        path, wanted = _inside(self.root, scope.get("path")), scope.get("record_ids")
        if path is None or not isinstance(wanted, list):
            return self._fail(ObservationOutcome.SCHEMA_INVALID, "scope_needs_path_and_record_ids", now)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return self._fail(ObservationOutcome.UNAVAILABLE, "file_missing", now, retry_after=60)
        if len(raw) > self.descriptor.max_payload_bytes:
            return self._fail(ObservationOutcome.SCHEMA_INVALID, "file_too_large", now)
        try:
            records = json.loads(raw)["records"]
            assert isinstance(records, dict) and all(isinstance(v, dict) for v in records.values())
        except (ValueError, KeyError, TypeError, AssertionError):
            return self._fail(ObservationOutcome.SCHEMA_INVALID, "not_a_records_file", now)
        found = {k: records[k] for k in wanted if k in records}
        gaps = tuple(k for k in wanted if k not in records)
        return ObservationResult(
            outcome=ObservationOutcome.PARTIAL if gaps else ObservationOutcome.SUCCESS,
            completeness=Completeness.PARTIAL if gaps else Completeness.COMPLETE,
            scope={"record_ids": list(wanted)}, records=found, observed_at=now, coverage_gaps=gaps)

    @staticmethod
    def _fail(outcome: ObservationOutcome, code: str, now: Any, retry_after: Optional[int] = None) -> ObservationResult:
        return ObservationResult(outcome=outcome, completeness=Completeness.UNKNOWN, scope={}, records={},
                                 observed_at=now, error_code=code, retry_after_seconds=retry_after)


class JournalTool:
    descriptor = ToolDescriptor(
        tool_id="example.journal", tool_version="0.1.0",
        input_schema={"type": "object", "required": ["file", "line"], "additionalProperties": False,
                      "properties": {"file": {"type": "string", "maxLength": 200},
                                     "line": {"type": "string", "maxLength": 2000}}},
        output_schema={"type": "object", "properties": {"line_no": {"type": "integer"}}},
        capability_type="files.append", allowed_resource_kinds=("file",),
        side_effect_class=SideEffectClass.LOCAL_WRITE, supports_idempotency=True,
        idempotency_retention_seconds=365 * 86400, supports_reconciliation=True,
        confirmation_semantics="line_with_effect_key", max_duration_seconds=5)

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self._lock = threading.Lock()

    def _find(self, path: Path, effect_key: str) -> Optional[int]:
        if not path.exists():
            return None
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.split("\t", 1)[0] == effect_key:
                return n
        return None

    def execute(self, request: AuthorizedAction) -> ActionResult:
        path = _inside(self.root, request.payload.get("file"))
        if path is None:
            return ActionResult(status="failed_no_effect", error="file_outside_root")
        text = str(request.payload.get("line", "")).replace("\n", " ")
        with self._lock:
            n = self._find(path, request.effect_key)
            if n is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(f"{request.effect_key}\t{text}\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                n = self._find(path, request.effect_key)
        return ActionResult(status="confirmed", provider_ref=f"line:{n}", provider_request_id=request.effect_key,
                            receipt={"line_no": n})

    def reconcile(self, request: ReconcileRequest) -> ReconcileResult:
        path = _inside(self.root, request.payload.get("file"))
        if path is None:
            return ReconcileResult(status="no_effect", evidence={"error": "file_outside_root"})
        n = self._find(path, request.effect_key)
        if n is None:
            return ReconcileResult(status="no_effect", evidence={"found": False})
        return ReconcileResult(status="confirmed", evidence={"line_no": n})


def _root(ctx: PluginContext) -> str:
    root = ctx.config.get("root") or os.environ.get("WAKECORE_EXAMPLE_ROOT")
    if not root:
        raise ValueError("set plugin config `root` (or WAKECORE_EXAMPLE_ROOT)")
    return str(root)


def make_connector(ctx: PluginContext) -> JsonFileSource:
    return JsonFileSource(_root(ctx))


def make_tool(ctx: PluginContext) -> JournalTool:
    return JournalTool(_root(ctx))
