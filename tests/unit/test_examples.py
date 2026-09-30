"""The example request bodies stay valid against the published schemas and the kernel's parsers."""
import json
from pathlib import Path

from wakecore.kernel.domain.taskspec import parse_task_spec
from wakecore.protocol.jsonschema_lite import Registry

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "web_watch_todomvc"


def load(name: str) -> dict:
    return json.loads((EXAMPLE / name).read_text(encoding="utf-8"))


def test_web_watch_task_spec_is_valid_and_parses():
    spec = load("task_spec.json")
    Registry.from_package("wakecore.protocol", "schemas").validate(spec, "task_spec.v1.schema.json")
    parsed = parse_task_spec(spec, tenant_id="local_user")
    assert parsed.task_id == "todo_watch"


def test_web_watch_extractor_is_valid_and_matches_the_spec():
    extractor = load("extractor.json")
    Registry.from_package("wakecore.protocol", "schemas/ui_runtime").validate(extractor, "extractor.v1.schema.json")
    assert load("task_spec.json")["source"]["resource_scope"]["extractor"] == extractor


def test_web_watch_grant_and_binding_cover_the_spec():
    spec, grant, binding = load("task_spec.json"), load("grant.json"), load("binding.json")
    auth = spec["authority"]
    assert grant["grant_ref"] == auth["grant_ref"]
    assert set(auth["read_capabilities"] + auth["notify_capabilities"]) <= set(grant["capabilities"])
    assert set(auth["model_data_egress"]) <= set(grant["data_egress"])
    assert binding["source_ref"] == spec["source"]["binding_ref"]
    assert spec["source"]["resource_scope"]["origins"] == grant["resource_scope"]["origins"] \
        == binding["resource_scope"]["origins"]
