"""Minimal JSON-Schema subset validator for versioned tool/port payloads (RFC §13.2).

Supported keywords: type, properties, required, additionalProperties (default false:
unknown fields are rejected, never passed through), items, enum, maxLength, minimum.
"""
from typing import Any

from .errors import SchemaMismatch

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def validate(schema: dict[str, Any], value: Any, path: str = "$") -> None:
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        ok = False
        for t in types:
            py = _TYPES[t]
            if t in ("integer", "number") and isinstance(value, bool):
                continue
            if isinstance(value, py):
                ok = True
                break
        if not ok:
            raise SchemaMismatch(f"{path}: expected {expected}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaMismatch(f"{path}: value not in enum")
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        raise SchemaMismatch(f"{path}: longer than {schema['maxLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and "minimum" in schema:
        if value < schema["minimum"]:
            raise SchemaMismatch(f"{path}: below minimum")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaMismatch(f"{path}.{key}: required")
        additional = schema.get("additionalProperties", False)
        for key, item in value.items():
            if key in props:
                validate(props[key], item, f"{path}.{key}")
            elif additional is False:
                raise SchemaMismatch(f"{path}.{key}: unknown field")
            elif isinstance(additional, dict):
                validate(additional, item, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            validate(schema["items"], item, f"{path}[{i}]")
