"""OpenAPI 3.1 documents generated from the live route tables (never written by hand).

    ui_runtime_openapi(ROUTES)          <- wakecore_ui_runtime.server.ROUTES
    ui_runtime_openapi(ROUTES, schemas="desktop_runtime", ...)  <- wakecore_ui_runtime.desktop.server.ROUTES
    kernel_api_openapi(api.routes)      <- wakecore.app.api.wsgi.WakeCoreAPI(...).routes

The JSON Schemas shipped in `wakecore/protocol/schemas` are embedded as components (OpenAPI 3.1
is JSON Schema 2020-12), with `$ref`s rewritten to `#/components/schemas/<doc>/...`. The
committed copies in `spec/` are checked against these functions by tests/contract/test_openapi_drift.py;
regenerate with `uv run python scripts/gen_openapi.py`.

Takes route tables as arguments so this module imports neither the sidecar nor the API.
"""
import copy
import json
import re
from typing import Any, Iterable

from wakecore import __version__

from .jsonschema_lite import registry

OPENAPI = "3.1.0"
LICENSE = {"name": "MIT", "identifier": "MIT"}
_PATH_PARAM = re.compile(r"\(\?P<(\w+)>\[\^/\]\+\)")


def _component_name(doc: str) -> str:
    return doc[:-len(".schema.json")] if doc.endswith(".schema.json") else doc


def _rewrite(node: Any, doc: str) -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in ("$id", "$schema"):
                continue                      # a nested $id would change how "#/..." resolves
            if k == "$ref" and isinstance(v, str):
                target, _, pointer = v.partition("#")
                out[k] = f"#/components/schemas/{_component_name(target or doc)}{pointer}"
            else:
                out[k] = _rewrite(v, doc)
        return out
    if isinstance(node, list):
        return [_rewrite(v, doc) for v in node]
    return node


def _components(reg_name: str) -> dict[str, Any]:
    reg = registry(reg_name)
    return {_component_name(name): _rewrite(copy.deepcopy(doc), name) for name, doc in sorted(reg.docs.items())}


def _ref(schema_ref: str) -> dict[str, str]:
    doc, _, pointer = schema_ref.partition("#")
    return {"$ref": f"#/components/schemas/{_component_name(doc)}{pointer}"}


def _json(schema: dict[str, Any], description: str = "") -> dict[str, Any]:
    return {**({"description": description} if description else {}),
            "content": {"application/json": {"schema": schema}}}


def _op_id(method: str, template: str) -> str:
    parts = [p.strip("{}") for p in template.split("/") if p and p != "v1"]
    return method.lower() + "_" + "_".join(re.sub(r"[^A-Za-z0-9]", "_", p) for p in parts)


# ====================================================================== UI Runtime

def ui_runtime_openapi(routes: Iterable[Any], *, protocol: str = "wakecore.ui-runtime/1",
                       runtime_version: str = __version__, schemas: str = "ui_runtime",
                       title: str = "WakeCore UI Runtime",
                       summary: str = "Browser sidecar: deterministic eye (Playwright) and model-driven hand "
                                      "(Computer Use)",
                       port: str = "8765") -> dict[str, Any]:
    """Also used for the desktop runtime (schemas="desktop_runtime"): same route shape, other contract."""
    error = _json(_ref("error.v1.schema.json"), "Error envelope (see error.code)")
    paths: dict[str, Any] = {}
    for r in routes:
        op: dict[str, Any] = {"operationId": _op_id(r.method, r.template), "summary": r.summary or r.template,
                              "responses": {"200": {**_json(_ref(r.response), "OK"),
                                                    "headers": {"WakeCore-Protocol": {"$ref": "#/components/headers/Protocol"}}},
                                            "default": error}}
        params = [{"name": n, "in": "path", "required": True, "schema": {"type": "string", "maxLength": 200}}
                  for n in re.findall(r"\{(\w+)\}", r.template)]
        params += [{"name": n, "in": "query", "required": True, "schema": {"type": "string", "maxLength": 200}}
                   for n in getattr(r, "query", ())]
        if params:
            op["parameters"] = params
        if r.schema:
            op["requestBody"] = {"required": True, **_json(_ref(r.schema))}
        if getattr(r, "deprecated", False):
            op["deprecated"] = True
        paths.setdefault(r.template, {})[r.method.lower()] = op
    return {
        "openapi": OPENAPI,
        "info": {"title": title, "version": runtime_version, "license": LICENSE, "summary": summary,
                 "x-wakecore-protocol": protocol},
        "servers": [{"url": "http://127.0.0.1:{port}", "description": "loopback only",
                     "variables": {"port": {"default": port}}}],
        "security": [{"bearer": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer",
                                           "description": "Required when the runtime was started with --token"}},
            "headers": {"Protocol": {"description": "Protocol family and major version, e.g. " + protocol,
                                     "schema": {"type": "string"}}},
            "schemas": _components(schemas),
        },
    }


# ====================================================================== Kernel management API

# Bodies with a published schema; everything else is documented by the handler's validation.
KERNEL_REQUESTS = {
    "POST /v1/tasks": "task_spec.v1.schema.json",
    "POST /v1/ingress/{id}": "event_envelope.v1.schema.json",
}
KERNEL_SUMMARIES = {
    "POST /v1/tasks": "Create a draft task (TaskSpec)",
    "GET /v1/tasks": "List the caller's tasks",
    "POST /v1/tasks/{id}/activate": "Activate a confirmed spec version (digest-bound)",
    "POST /v1/tasks/{id}/pause": "Pause a task", "POST /v1/tasks/{id}/resume": "Resume a task",
    "POST /v1/tasks/{id}/cancel": "Cancel a task",
    "GET /v1/tasks/{id}": "Task state and explanation (ETag = version)",
    "GET /v1/tasks/{id}/timeline": "Audit timeline of a task",
    "GET /v1/runs/{id}": "One run", "GET /v1/evidence/{id}": "One evidence record",
    "POST /v1/approvals/{id}/approve": "Approve an action (binds payload_digest)",
    "POST /v1/approvals/{id}/reject": "Reject an action",
    "POST /v1/actions/{id}/revise": "Replace an action's payload (needs a new approval)",
    "POST /v1/actions/{id}/resolve": "Resolve an UNKNOWN action by hand",
    "POST /v1/grants": "Register a grant (capabilities, data egress, resource scope)",
    "POST /v1/grants/{id}/revoke": "Revoke a grant",
    "POST /v1/bindings": "Register a source binding",
    "POST /v1/bindings/{id}/reauthorised": "Mark a source as re-authorised after a login",
    "GET /v1/tools": "Installed tools, with their MCP form (installed is not granted)",
    "GET /v1/connectors": "Installed connectors",
    "POST /v1/ingress/{id}": "Push an event (HMAC-signed, cannot control tasks)",
    "GET /healthz": "Liveness",
}


def route_template(pattern: str) -> str:
    """`/v1/tasks/(?P<id>[^/]+)/?` -> `/v1/tasks/{id}`."""
    p = pattern[:-2] if pattern.endswith("/?") else pattern
    return _PATH_PARAM.sub(lambda m: "{" + m.group(1) + "}", p)


def kernel_api_openapi(routes: Iterable[tuple[str, Any, bool, Any]], *, version: str = __version__) -> dict[str, Any]:
    error = _json(_ref("api_error.v1.schema.json"), "Error envelope (code, retryable, trace_id)")
    obj = {"type": "object"}
    paths: dict[str, Any] = {}
    for method, pattern, authenticated, _ in routes:
        template = route_template(getattr(pattern, "pattern", pattern))
        key = f"{method} {template}"
        op: dict[str, Any] = {"operationId": _op_id(method, template), "summary": KERNEL_SUMMARIES.get(key, key),
                              "responses": {"2XX": _json(obj, "OK (201 on create, 202 when ingress accepts)"),
                                            "default": error}}
        params = [{"name": n, "in": "path", "required": True, "schema": {"type": "string"}}
                  for n in re.findall(r"\{(\w+)\}", template)]
        if method == "POST" and authenticated:
            params.append({"name": "Idempotency-Key", "in": "header", "required": False,
                           "schema": {"type": "string", "minLength": 1, "maxLength": 128},
                           "description": "Same key + same request replays the first response"})
        if template.startswith("/v1/tasks/{id}") and method == "POST":
            params.append({"name": "If-Match", "in": "header", "required": False, "schema": {"type": "string"},
                           "description": "Expected task version (or body expected_version)"})
        if not authenticated and template.startswith("/v1/ingress"):
            params.append({"name": "X-WakeCore-Signature", "in": "header", "required": True,
                           "schema": {"type": "string"}, "description": "HMAC-SHA256 of the body"})
        if params:
            op["parameters"] = params
        if method == "POST":
            schema = _ref(KERNEL_REQUESTS[key]) if key in KERNEL_REQUESTS else obj
            if key == "POST /v1/tasks":     # the spec itself, or {"spec": ...}
                schema = {"anyOf": [schema, {"type": "object", "required": ["spec"], "properties": {"spec": schema}}]}
            op["requestBody"] = {"required": key in KERNEL_REQUESTS, **_json(schema)}
        op["security"] = [{"bearer": []}] if authenticated else []
        paths.setdefault(template, {})[method.lower()] = op
    return {
        "openapi": OPENAPI,
        "info": {"title": "WakeCore management and ingress API", "version": version, "license": LICENSE,
                 "summary": "Tasks, approvals, grants, bindings and signed ingress; workers do the work"},
        "servers": [{"url": "http://127.0.0.1:8080"}],
        "paths": paths,
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "schemas": _components("kernel"),
        },
    }


def dumps(doc: dict[str, Any]) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
