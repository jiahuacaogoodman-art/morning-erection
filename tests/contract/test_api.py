"""RFC §15 API contract: auth, error shape, ETag/If-Match, idempotency, ingress separation,
and that no secret, token or request body is echoed back."""
import io
import json

import pytest

from harness import TENANT, USER, base_spec
from wakecore.app.api.wsgi import MAX_BODY, TokenAuth, WakeCoreAPI
from wakecore.kernel.service import KernelService, Principal

TOKEN = "tok-alice-7f3c9a"
BOB_TOKEN = "tok-bob-1b2c3d"
SECRETS = ("session-cookie-demo", "ingress-hmac-demo", TOKEN, BOB_TOKEN)


class Client:
    def __init__(self, h):
        self.h = h
        self.app = WakeCoreAPI(KernelService(h.ctx), TokenAuth.from_tokens(
            {TOKEN: Principal(TENANT, USER), BOB_TOKEN: Principal("tenant_b", "user:bob")}))

    def __call__(self, method, path, body=None, *, token=TOKEN, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_LENGTH": str(len(data)),
               "wsgi.input": io.BytesIO(data)}
        if token:
            env["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        for k, v in (headers or {}).items():
            env["HTTP_" + k.upper().replace("-", "_")] = v
        got = {}

        def start_response(status, hdrs):
            got["status"] = int(status.split()[0])
            got["headers"] = dict(hdrs)

        out = b"".join(self.app(env, start_response))
        text = out.decode()
        for s in SECRETS:
            assert s not in text, f"secret material leaked in {method} {path}"
        return got["status"], got["headers"], json.loads(text)


@pytest.fixture
def api(h):
    h.grant()
    h.binding()
    return Client(h)


def created(api, spec=None, key="c1"):
    st, _, body = api("POST", "/v1/tasks", {"spec": spec or base_spec()}, headers={"Idempotency-Key": key})
    assert st == 201, body
    return body


def active(api):
    c = created(api)
    st, _, body = api("POST", f"/v1/tasks/{c['task_id']}/activate",
                      {"spec_version": c["spec_version"], "spec_digest": c["spec_digest"]})
    assert st == 200, body
    return c["task_id"]


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_management_requires_bearer_token(api, token):
    st, hdrs, body = api("GET", "/v1/tasks", token=token)
    assert st == 401 and body["error"]["code"] == "UNAUTHENTICATED"
    assert body["error"]["trace_id"] == hdrs["X-Trace-Id"]


def test_error_shape_and_trace_id(api):
    st, hdrs, body = api("GET", "/v1/tasks/nope", headers={"X-Trace-Id": "trace-abc.1"})
    assert st == 404 and hdrs["X-Trace-Id"] == "trace-abc.1"
    assert set(body["error"]) == {"code", "message", "retryable", "details", "trace_id"}
    assert body["error"]["trace_id"] == "trace-abc.1" and body["error"]["retryable"] is False
    st, hdrs, _ = api("GET", "/v1/tasks/nope", headers={"X-Trace-Id": "bad id\r\nSet-Cookie: x"})
    assert "\n" not in hdrs["X-Trace-Id"] and hdrs["X-Trace-Id"] != "bad id\r\nSet-Cookie: x"


def test_unknown_endpoint_and_wrong_method(api):
    assert api("GET", "/v1/nothing")[0] == 404
    st, _, body = api("DELETE", "/v1/tasks")
    assert st == 405 and body["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_oversized_and_malformed_bodies(api):
    st, _, body = api("POST", "/v1/tasks", raw=b"x" * (MAX_BODY + 1))
    assert st == 413
    st, _, body = api("POST", "/v1/tasks", raw=b"{not json")
    assert st == 422 or st == 400
    assert "not json" not in json.dumps(body)       # bodies are never echoed


def test_create_is_idempotent_and_conflicting_reuse_is_rejected(api):
    a = api("POST", "/v1/tasks", {"spec": base_spec()}, headers={"Idempotency-Key": "k"})
    b = api("POST", "/v1/tasks", {"spec": base_spec()}, headers={"Idempotency-Key": "k"})
    assert a[0] == b[0] == 201 and a[2] == b[2]
    assert b[1].get("Idempotent-Replayed") == "true"
    st, _, body = api("POST", "/v1/tasks", {"spec": base_spec(purpose="different")},
                      headers={"Idempotency-Key": "k"})
    assert st == 409 and body["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_etag_and_if_match(api):
    tid = active(api)
    st, hdrs, task = api("GET", f"/v1/tasks/{tid}")
    assert hdrs["ETag"] == f'"{task["version"]}"'
    st, _, body = api("POST", f"/v1/tasks/{tid}/pause", {}, headers={"If-Match": f'"{task["version"] + 5}"'})
    assert st == 409 and body["error"]["code"] == "VERSION_CONFLICT"
    st, _, body = api("POST", f"/v1/tasks/{tid}/pause", {"expected_version": 1},
                      headers={"If-Match": '"2"'})
    assert st in (400, 422)
    st, _, body = api("POST", f"/v1/tasks/{tid}/pause", {}, headers={"If-Match": hdrs["ETag"]})
    assert st == 200 and body["lifecycle"] == "PAUSED"


def test_approve_requires_payload_digest(api):
    st, _, body = api("POST", "/v1/approvals/apv_x/approve", {})
    assert st in (400, 422) and "payload_digest" in body["error"]["message"]


def test_other_tenant_sees_nothing(api):
    tid = active(api)
    st, _, body = api("GET", f"/v1/tasks/{tid}", token=BOB_TOKEN)
    assert st == 404
    st, _, body = api("GET", "/v1/tasks", token=BOB_TOKEN)
    assert body["items"] == []


def test_ingress_uses_signature_not_bearer_and_cannot_control(api):
    tid = active(api)
    h = api.h
    body = h.envelope("evt-api-1")
    # a bearer token does not authenticate ingress
    st, _, out = api("POST", "/v1/ingress/school_account_demo", raw=body)
    assert st == 401
    st, _, out = api("POST", "/v1/ingress/school_account_demo", raw=body, token=None,
                     headers={"X-Wakecore-Signature": h.signature(body)})
    assert st in (200, 202) and out["status"] == "accepted"
    # an ingress-shaped control attempt is still just an (unrelated-type) event, never a command
    evil = json.dumps({"specversion": "1.0", "id": "e2", "source": "urn:wakecore:source:school-demo",
                       "type": "wakecore.task.cancel", "data": {"task_id": tid}}).encode()
    st, _, out = api("POST", "/v1/ingress/school_account_demo", raw=evil, token=None,
                     headers={"X-Wakecore-Signature": h.signature(evil)})
    assert st == 403
    assert api("GET", f"/v1/tasks/{tid}")[2]["lifecycle"] == "ACTIVE"


def test_binding_view_never_contains_secret_values(api):
    st, _, body = api("POST", "/v1/bindings", {"source_ref": "src2", "connector_id": "offline.grades",
                                               "source_uri": "urn:x", "secret_ref": "sec_school"})
    assert st == 201 and "secret_ref" not in body and "ingress_secret_ref" not in body


def test_healthz_needs_no_auth(api):
    assert api("GET", "/healthz", token=None)[0] == 200


def test_cli_demo_runs_offline(capsys, tmp_path):
    from wakecore.app.cli.main import main

    assert main(["--db", f"sqlite:///{tmp_path / 'demo.db'}", "demo"]) == 0
    out = capsys.readouterr().out
    assert "外部写入 0 次" in out and "COMPLETED" in out


# ---------------------------------------------------------------------- installed adapters

def test_tools_listing_shows_descriptors_and_mcp_form(api):
    st, _, body = api("GET", "/v1/tools")
    assert st == 200
    by_id = {t["tool_id"]: t for t in body["items"]}
    assert {"inbox.notify", "email.send"} <= set(by_id)
    assert [t["tool_id"] for t in body["items"]] == sorted(by_id)
    inbox = by_id["inbox.notify"]
    assert inbox["side_effect_class"] == "local_write" and inbox["enabled"] is True
    assert inbox["mcp"]["name"] == "inbox.notify"
    assert inbox["mcp"]["annotations"]["readOnlyHint"] is False
    assert inbox["mcp"]["inputSchema"]["additionalProperties"] is False
    email = by_id["email.send"]
    assert email["mcp"]["annotations"]["destructiveHint"] is True
    assert email["mcp"]["_meta"]["dev.wakecore/approval_default"] == "required"


def test_connectors_listing(api):
    st, _, body = api("GET", "/v1/connectors")
    assert st == 200
    grades = {c["connector_id"]: c for c in body["items"]}["offline.grades"]
    assert grades["read_capability"] == "grades.read" and grades["observation_mode"] == "snapshot"
    assert "enabled" in grades


@pytest.mark.parametrize("path", ["/v1/tools", "/v1/connectors"])
def test_adapter_listings_need_a_token(api, path):
    st, _, body = api("GET", path, token=None)
    assert st == 401 and body["error"]["code"] == "UNAUTHENTICATED"
