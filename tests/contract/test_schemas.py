"""schemas/*.json stay in step with the parsers they document.

The parsers are authoritative; these tests only stop the published schemas drifting. A
minimal validator (the subset of JSON Schema the files use) keeps the kernel stdlib-only.
"""
import json
import pathlib
import re

import pytest

from harness import base_spec, email_spec
from wakecore.kernel.domain import errors, taskspec
from wakecore.kernel.domain.enums import CatchupPolicy, TriggerKind
from wakecore.kernel.domain.errors import SchemaMismatch

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "packages" / "wakecore" / "src" / "wakecore"


def load(name):
    return json.loads((SRC / "protocol" / "schemas" / name).read_text(encoding="utf-8"))


def validate(schema, value, path="$"):
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: {value!r} not in enum")
    t = schema.get("type")
    kinds = {"object": dict, "array": list, "string": str, "boolean": bool}
    if t == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValueError(f"{path}: expected integer")
    if t in kinds and not isinstance(value, kinds[t]):
        raise ValueError(f"{path}: expected {t}")
    if t == "integer" and value < schema.get("minimum", value):
        raise ValueError(f"{path}: below minimum")
    if t == "string":
        if len(value) < schema.get("minLength", 0):
            raise ValueError(f"{path}: too short")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ValueError(f"{path}: pattern")
        if "not" in schema and "pattern" in schema["not"] and re.search(schema["not"]["pattern"], value):
            raise ValueError(f"{path}: forbidden pattern")
    if t == "array":
        for i, v in enumerate(value):
            validate(schema.get("items", {}), v, f"{path}[{i}]")
    if t == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{path}.{key}: required")
        for key, v in value.items():
            if key in props:
                validate(props[key], v, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise ValueError(f"{path}.{key}: unknown field")


# ------------------------------------------------------------------ TaskSpec

SPEC = load("task_spec.v1.schema.json")


def test_taskspec_schema_field_sets_match_the_parser():
    p = SPEC["properties"]
    assert set(p) == taskspec._TOP
    assert set(p["limits"]["properties"]) == set(taskspec.Limits.__dataclass_fields__)
    assert set(p["trigger"]["properties"]["kind"]["enum"]) == {k.value for k in TriggerKind}
    assert set(p["trigger"]["properties"]["catchup_policy"]["enum"]) == {c.value for c in CatchupPolicy}
    assert set(p["observation"]["properties"]["first_snapshot"]["enum"]) == taskspec.FIRST_SNAPSHOT_POLICIES
    assert set(p["observation"]["properties"]["completeness_required"]["enum"]) == taskspec.COMPLETENESS_POLICIES
    from wakecore.kernel.decision.profiles import PROFILES
    assert set(p["decision"]["properties"]["profile"]["enum"]) == set(PROFILES)


@pytest.mark.parametrize("spec", [base_spec(), email_spec(),
                                  json.loads((SRC / "app" / "cli" / "grades_demo.json").read_text(encoding="utf-8"))])
def test_accepted_specs_validate(spec):
    validate(SPEC, spec)
    taskspec.parse_task_spec(spec, tenant_id="local_user")


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(surprise=1),
    lambda s: s["authority"].update(admin=True),
    lambda s: s["trigger"].update(kind="cron"),
    lambda s: s["limits"].update(max_steps_per_run=-1),
    lambda s: s.pop("expires_at"),
    lambda s: s.update(schema_version=2),
])
def test_schema_and_parser_reject_the_same_mistakes(mutate):
    spec = base_spec()
    mutate(spec)
    with pytest.raises(ValueError):
        validate(SPEC, spec)
    with pytest.raises(SchemaMismatch):
        taskspec.parse_task_spec(spec, tenant_id="local_user")


# ------------------------------------------------------------------ envelope / errors

def test_harness_envelope_validates(h):
    validate(load("event_envelope.v1.schema.json"), json.loads(h.envelope("evt-1")))
    with pytest.raises(ValueError):
        validate(load("event_envelope.v1.schema.json"), json.loads(h.envelope("evt-2", type_="wakecore.fake")))


def test_error_codes_cover_the_taxonomy():
    codes = set(load("api_error.v1.schema.json")["properties"]["error"]["properties"]["code"]["enum"])
    kernel = {c.code for c in vars(errors).values() if isinstance(c, type) and issubclass(c, errors.KernelError)}
    api = set(re.findall(r'"([A-Z_]+)"', (SRC / "app/api/wsgi.py").read_text(encoding="utf-8")))
    assert kernel <= codes
    assert codes - kernel == {"METHOD_NOT_ALLOWED", "PAYLOAD_TOO_LARGE", "INTERNAL"} <= api


def test_error_response_validates(h):
    import io

    from wakecore.app.api.wsgi import TokenAuth, WakeCoreAPI
    from wakecore.kernel.service import KernelService

    app = WakeCoreAPI(KernelService(h.ctx), TokenAuth({}))
    out = {}
    body = b"".join(app({"REQUEST_METHOD": "GET", "PATH_INFO": "/v1/tasks", "wsgi.input": io.BytesIO()},
                        lambda status, headers: out.update(status=status)))
    assert out["status"].startswith("401")
    validate(load("api_error.v1.schema.json"), json.loads(body))
