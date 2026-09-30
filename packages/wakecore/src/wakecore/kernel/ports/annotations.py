"""Export descriptors in neutral, JSON-ready form, including an MCP `Tool` view.

`to_mcp_tool` maps a ToolDescriptor onto the Model Context Protocol tool definition
(name / inputSchema / outputSchema / annotations / _meta). MCP annotations are *hints* for
clients; they grant nothing. WakeCore's own guarantees (grants, approval bound to the payload
digest, origin scope, send-once) stay in the kernel and are carried in `_meta` under the
`dev.wakecore/` prefix so a client can display them.

    readOnlyHint     side_effect_class == read
    destructiveHint  external_write (effects outside WakeCore may not be undoable); local_write is additive
    idempotentHint   the tool itself dedupes by effect key (supports_idempotency)
    openWorldHint    it talks to something outside the kernel: external_write, url_fields or required_egress

Pure functions: no MCP SDK, nothing is served from here.
"""
import copy
from typing import Any

from ..domain.enums import SideEffectClass
from .action import ToolDescriptor
from .observation import ConnectorDescriptor

META = "dev.wakecore/"


def _closed(schema: Any) -> Any:
    """The kernel's payload validator treats a missing additionalProperties as false; say so explicitly."""
    if not isinstance(schema, dict):
        return schema
    out = copy.deepcopy(schema)
    if out.get("type") == "object" and "additionalProperties" not in out:
        out["additionalProperties"] = False
    if isinstance(out.get("properties"), dict):
        out["properties"] = {k: _closed(v) for k, v in out["properties"].items()}
    if isinstance(out.get("items"), dict):
        out["items"] = _closed(out["items"])
    if isinstance(out.get("additionalProperties"), dict):
        out["additionalProperties"] = _closed(out["additionalProperties"])
    return out


def tool_view(d: ToolDescriptor) -> dict[str, Any]:
    return {
        "tool_id": d.tool_id, "tool_version": d.tool_version, "capability": d.capability_type,
        "side_effect_class": d.side_effect_class.value, "allowed_resource_kinds": list(d.allowed_resource_kinds),
        "supports_idempotency": d.supports_idempotency,
        "idempotency_retention_seconds": d.idempotency_retention_seconds,
        "supports_reconciliation": d.supports_reconciliation, "confirmation_semantics": d.confirmation_semantics,
        "max_duration_seconds": d.max_duration_seconds, "retry_owner": d.retry_owner,
        "data_egress_policy": d.data_egress_policy, "url_fields": list(d.url_fields),
        "required_egress": list(d.required_egress), "credential": d.credential,
        "input_schema": _closed(d.input_schema), "output_schema": copy.deepcopy(d.output_schema),
    }


def connector_view(d: ConnectorDescriptor) -> dict[str, Any]:
    return {
        "connector_id": d.connector_id, "version": d.version, "read_capability": d.read_capability,
        "supported_resource_types": list(d.supported_resource_types), "observation_mode": d.observation_mode.value,
        "supports_history_replay": d.supports_history_replay, "supports_entity_revision": d.supports_entity_revision,
        "authentication_model": d.authentication_model, "required_scopes": list(d.required_scopes),
        "rate_limit_per_minute": d.rate_limit_per_minute, "max_payload_bytes": d.max_payload_bytes,
        "snapshot_completeness_contract": d.snapshot_completeness_contract,
    }


def mcp_annotations(d: ToolDescriptor) -> dict[str, bool]:
    read_only = d.side_effect_class is SideEffectClass.READ
    external = d.side_effect_class is SideEffectClass.EXTERNAL_WRITE
    return {
        "readOnlyHint": read_only,
        "destructiveHint": external,
        "idempotentHint": read_only or d.supports_idempotency,
        "openWorldHint": external or bool(d.url_fields) or bool(d.required_egress),
    }


def to_mcp_tool(d: ToolDescriptor, *, title: str = "", description: str = "") -> dict[str, Any]:
    tool: dict[str, Any] = {"name": d.tool_id, "inputSchema": _closed(d.input_schema)}
    if title:
        tool["title"] = title
    if description:
        tool["description"] = description
    if isinstance(d.output_schema, dict) and d.output_schema.get("type") == "object":
        tool["outputSchema"] = copy.deepcopy(d.output_schema)
    tool["annotations"] = {**({"title": title} if title else {}), **mcp_annotations(d)}
    tool["_meta"] = {
        META + "tool_version": d.tool_version,
        META + "capability": d.capability_type,
        META + "side_effect_class": d.side_effect_class.value,
        META + "confirmation_semantics": d.confirmation_semantics,
        META + "supports_reconciliation": d.supports_reconciliation,
        META + "url_fields": list(d.url_fields),
        META + "required_egress": list(d.required_egress),
        META + "credential": d.credential,
        META + "approval_default": "required" if d.side_effect_class is SideEffectClass.EXTERNAL_WRITE else "not_required",
    }
    return tool
