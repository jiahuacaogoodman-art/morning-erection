"""The committed OpenAPI documents are generated, never edited: they must match the route tables.

Regenerate with `uv run python scripts/gen_openapi.py` after changing a route or a schema.
"""
import json
import re
from pathlib import Path

import pytest

from wakecore.app.api.wsgi import TokenAuth, WakeCoreAPI
from wakecore.app.cli.main import kernel_openapi
from wakecore.protocol.openapi import dumps, route_template

ROOT = Path(__file__).resolve().parents[2]
KERNEL_SPEC = ROOT / "spec" / "kernel-api" / "openapi.v1.json"
UI_SPEC = ROOT / "spec" / "ui-runtime" / "openapi.v1.json"
DESKTOP_SPEC = ROOT / "spec" / "desktop-runtime" / "openapi.v1.json"
HINT = "stale; run `uv run python scripts/gen_openapi.py`"


def _refs(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref":
                yield v
            else:
                yield from _refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from _refs(v)


def _resolve(doc, ref):
    assert ref.startswith("#/"), ref
    node = doc
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def _no_dangling_refs(doc):
    for ref in _refs(doc):
        _resolve(doc, ref)                          # KeyError = a ref the rewrite got wrong


def _ui():
    return pytest.importorskip("wakecore_ui_runtime.server")


# ---------------------------------------------------------------------- kernel API

def test_kernel_spec_is_up_to_date():
    assert KERNEL_SPEC.read_text(encoding="utf-8") == dumps(kernel_openapi()), HINT


def test_kernel_spec_covers_every_route_and_nothing_else():
    doc = json.loads(KERNEL_SPEC.read_text(encoding="utf-8"))
    routes = {(m.lower(), route_template(p.pattern)) for m, p, _, _ in WakeCoreAPI(None, TokenAuth({})).routes}
    documented = {(m, path) for path, ops in doc["paths"].items() for m in ops}
    assert routes == documented


def test_kernel_spec_security_matches_authentication():
    doc = json.loads(KERNEL_SPEC.read_text(encoding="utf-8"))
    for m, p, authenticated, _ in WakeCoreAPI(None, TokenAuth({})).routes:
        op = doc["paths"][route_template(p.pattern)][m.lower()]
        assert op["security"] == ([{"bearer": []}] if authenticated else []), (m, p.pattern)
    ingress = doc["paths"]["/v1/ingress/{id}"]["post"]
    assert any(x["name"] == "X-WakeCore-Signature" and x["required"] for x in ingress["parameters"])


def test_kernel_spec_refs_resolve_and_errors_use_the_envelope():
    doc = json.loads(KERNEL_SPEC.read_text(encoding="utf-8"))
    _no_dangling_refs(doc)
    for path, ops in doc["paths"].items():
        for m, op in ops.items():
            schema = op["responses"]["default"]["content"]["application/json"]["schema"]
            assert schema == {"$ref": "#/components/schemas/api_error.v1"}, (m, path)
    assert "$id" not in json.dumps(doc["components"]["schemas"])


def test_route_template():
    assert route_template(r"/v1/tasks/(?P<id>[^/]+)/timeline/?") == "/v1/tasks/{id}/timeline"
    assert route_template(r"/healthz/?") == "/healthz"


# ---------------------------------------------------------------------- UI runtime

def test_ui_runtime_spec_is_up_to_date():
    server = _ui()
    assert UI_SPEC.read_text(encoding="utf-8") == server.openapi_json(), HINT


def test_ui_runtime_spec_covers_every_route():
    server = _ui()
    doc = json.loads(UI_SPEC.read_text(encoding="utf-8"))
    routes = {(r.method.lower(), r.template) for r in server.ROUTES}
    assert routes == {(m, p) for p, ops in doc["paths"].items() for m in ops}
    assert doc["info"]["x-wakecore-protocol"] == server.PROTOCOL


def test_ui_runtime_every_route_declares_request_and_response_schemas():
    server = _ui()
    doc = json.loads(UI_SPEC.read_text(encoding="utf-8"))
    _no_dangling_refs(doc)
    for r in server.ROUTES:
        op = doc["paths"][r.template][r.method.lower()]
        ok = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        assert isinstance(_resolve(doc, ok), dict), (r.method, r.template)
        assert op["responses"]["default"]["content"]["application/json"]["schema"] == \
            {"$ref": "#/components/schemas/error.v1"}
        assert ("requestBody" in op) == (r.schema is not None), (r.method, r.template)
        assert {p["name"] for p in op.get("parameters", []) if p["in"] == "path"} == \
            set(re.findall(r"\{(\w+)\}", r.template))
    assert doc["paths"]["/v1/act/status"]["get"]["deprecated"] is True
    assert doc["paths"]["/v1/sessions/release"]["post"]["deprecated"] is True


# ---------------------------------------------------------------------- desktop runtime

def test_desktop_runtime_spec_is_up_to_date_and_complete():
    server = pytest.importorskip("wakecore_ui_runtime.desktop.server")
    assert DESKTOP_SPEC.read_text(encoding="utf-8") == server.openapi_json(), HINT
    doc = json.loads(DESKTOP_SPEC.read_text(encoding="utf-8"))
    _no_dangling_refs(doc)
    assert {(r.method.lower(), r.template) for r in server.ROUTES} == \
        {(m, p) for p, ops in doc["paths"].items() for m in ops}
    assert doc["info"]["x-wakecore-protocol"] == server.PROTOCOL
    for r in server.ROUTES:
        op = doc["paths"][r.template][r.method.lower()]
        assert isinstance(_resolve(doc, op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]), dict)
        assert ("requestBody" in op) == (r.schema is not None), (r.method, r.template)
