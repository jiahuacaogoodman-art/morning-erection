"""The stdlib JSON Schema subset used for WakeCore's published contracts."""
import pytest

from wakecore.protocol.jsonschema_lite import Registry, SchemaError, registry

DOCS = {
    "a.schema.json": {
        "type": "object", "additionalProperties": False, "required": ["n"],
        "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 3},
                       "x": {"type": "number"}, "tags": {"type": "array", "items": {"$ref": "#/$defs/tag"},
                                                         "uniqueItems": True, "maxItems": 2},
                       "b": {"$ref": "b.schema.json#/$defs/b"}, "maybe": {"type": ["string", "null"]},
                       "pick": {"oneOf": [{"const": 1}, {"type": "string"}]},
                       "no": {"not": {"const": "forbidden"}}, "m": {"type": "object",
                                                                    "propertyNames": {"pattern": "^[a-z]+$"},
                                                                    "additionalProperties": {"type": "boolean"}}},
        "$defs": {"tag": {"type": "string", "pattern": "^[a-z]+$", "minLength": 2}}},
    "b.schema.json": {"$defs": {"b": {"enum": ["yes", "no"]}}},
}


def v(value):
    Registry(DOCS).validate(value, "a.schema.json")


@pytest.mark.parametrize("value", [
    {"n": 1}, {"n": 3, "x": 1.5, "tags": ["ab", "cd"], "b": "yes", "maybe": None, "pick": 1},
    {"n": 2, "x": 2, "pick": "s", "no": "fine", "m": {"ok": True}},
])
def test_accepts(value):
    v(value)


@pytest.mark.parametrize("value,path", [
    ({}, "$.n"), ({"n": 0}, "$.n"), ({"n": 4}, "$.n"), ({"n": True}, "$.n"), ({"n": 1.0}, "$.n"),
    ({"n": 1, "x": True}, "$.x"), ({"n": 1, "zz": 1}, "$.zz"), ({"n": 1, "tags": ["ab", "ab"]}, "$.tags"),
    ({"n": 1, "tags": ["a"]}, "$.tags[0]"), ({"n": 1, "tags": ["AB"]}, "$.tags[0]"),
    ({"n": 1, "tags": ["ab", "cd", "ef"]}, "$.tags"), ({"n": 1, "b": "maybe"}, "$.b"),
    ({"n": 1, "maybe": 3}, "$.maybe"), ({"n": 1, "pick": 2}, "$.pick"), ({"n": 1, "no": "forbidden"}, "$.no"),
    ({"n": 1, "m": {"Bad": True}}, "$.m.Bad"), ({"n": 1, "m": {"ok": "yes"}}, "$.m.ok"), ([], "$"),
])
def test_rejects_with_a_path(value, path):
    with pytest.raises(SchemaError) as e:
        v(value)
    assert e.value.path == path


def test_one_of_means_exactly_one():
    r = Registry({"s.json": {"oneOf": [{"type": "integer"}, {"type": "number"}]}})
    assert not r.is_valid(1, "s.json") and r.is_valid(1.5, "s.json")


def test_check_refuses_unsupported_keywords_and_dangling_refs():
    with pytest.raises(KeyError, match="unsupported keyword 'if'"):
        Registry({"s.json": {"if": {"type": "string"}}}).check()
    with pytest.raises(KeyError, match="unknown schema document"):
        Registry({"s.json": {"$ref": "nope.json"}}).check()
    Registry(DOCS).check()


def test_shipped_schemas_are_self_consistent():
    registry("ui_runtime").check()
    registry("kernel").check()
    assert {"extractor.v1.schema.json", "act.v1.schema.json", "error.v1.schema.json"} <= set(registry().docs)
