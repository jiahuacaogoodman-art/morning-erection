"""A small, strict JSON Schema (2020-12 subset) validator, standard library only.

It covers exactly the keywords WakeCore's published schemas use, so the kernel stays
dependency-free and the UI Runtime can validate requests without pulling in `jsonschema`:

    type (incl. "number", "null", lists of types)   const  enum
    minimum  maximum  minLength  maxLength  pattern  minItems  maxItems  uniqueItems
    properties  required  additionalProperties (bool | schema)  propertyNames (pattern)
    items  $ref (local "#/$defs/x" or "<file>#/$defs/x" within one Registry)  $defs
    allOf  anyOf  oneOf  not

Unknown keywords are ignored (as JSON Schema does), except that `Registry.check()` refuses
schemas using keywords outside this list, so a schema can never silently mean more than the
validator enforces. Errors carry a JSON-pointer-ish path: `$.columns[2].type`.
"""
import json
import re
from importlib import resources
from typing import Any, Optional

SUPPORTED = frozenset({
    "$schema", "$id", "$ref", "$defs", "$comment", "title", "description", "examples", "default", "deprecated",
    "readOnly", "writeOnly", "format",
    "type", "const", "enum", "minimum", "maximum", "minLength", "maxLength", "pattern", "minItems", "maxItems",
    "uniqueItems", "properties", "required", "additionalProperties", "propertyNames", "items", "allOf", "anyOf",
    "oneOf", "not",
})
_TYPES: dict[str, Any] = {"object": dict, "array": list, "string": str, "boolean": bool}


class SchemaError(ValueError):
    """The value does not match the schema. `path` locates the offending part."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path, self.message = path, message


def _is_type(value: Any, t: str) -> bool:
    if t == "null":
        return value is None
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[t])


class Registry:
    """A set of schema documents that may `$ref` each other by file name."""

    def __init__(self, docs: Optional[dict[str, dict[str, Any]]] = None) -> None:
        self.docs: dict[str, dict[str, Any]] = dict(docs or {})

    @classmethod
    def from_package(cls, package: str, subdir: str = "") -> "Registry":
        root = resources.files(package)
        for part in [p for p in subdir.split("/") if p]:
            root = root.joinpath(part)
        docs = {p.name: json.loads(p.read_text(encoding="utf-8"))
                for p in root.iterdir() if p.name.endswith(".schema.json")}
        return cls(docs)

    # ------------------------------------------------------------------ refs
    def resolve(self, ref: str, base: str) -> tuple[dict[str, Any], str]:
        doc_name, _, pointer = ref.partition("#")
        doc_name = doc_name or base
        if doc_name not in self.docs:
            raise KeyError(f"unknown schema document {doc_name!r} in $ref {ref!r}")
        node: Any = self.docs[doc_name]
        for part in [p for p in pointer.split("/") if p]:
            node = node[part.replace("~1", "/").replace("~0", "~")]
        return node, doc_name

    def check(self) -> None:
        """Every $ref resolves and no schema uses a keyword this validator does not enforce."""
        def walk(node: Any, doc: str, at: str) -> None:
            if isinstance(node, dict):
                if "$ref" in node:
                    self.resolve(node["$ref"], doc)
                for k, v in node.items():
                    if k in ("properties", "$defs"):
                        for name, sub in v.items():
                            walk(sub, doc, f"{at}/{k}/{name}")
                    elif k in ("examples", "default", "const", "enum"):
                        continue
                    elif k not in SUPPORTED:
                        raise KeyError(f"{doc}{at}: unsupported keyword {k!r}")
                    else:
                        walk(v, doc, f"{at}/{k}")
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, doc, f"{at}/{i}")
        for name, doc in self.docs.items():
            walk(doc, name, "")

    # ------------------------------------------------------------------ validation
    def validate(self, value: Any, ref: str) -> None:
        """Validate against `"<file>"` or `"<file>#/$defs/name"`. Raises SchemaError."""
        schema, doc = self.resolve(ref, "")
        self._v(schema, value, "$", doc)

    def is_valid(self, value: Any, ref: str) -> bool:
        try:
            self.validate(value, ref)
        except SchemaError:
            return False
        return True

    def _v(self, s: Any, value: Any, path: str, doc: str) -> None:
        if s is True or s == {}:
            return
        if s is False:
            raise SchemaError(path, "not allowed")
        if "$ref" in s:
            target, tdoc = self.resolve(s["$ref"], doc)
            self._v(target, value, path, tdoc)
        if "const" in s and value != s["const"]:
            raise SchemaError(path, f"expected {s['const']!r}")
        if "enum" in s and value not in s["enum"]:
            raise SchemaError(path, f"{value!r} is not one of {s['enum']!r}")
        t = s.get("type")
        if t is not None:
            kinds = t if isinstance(t, list) else [t]
            if not any(_is_type(value, k) for k in kinds):
                raise SchemaError(path, f"expected {' or '.join(kinds)}")
        if _is_type(value, "number"):
            if "minimum" in s and value < s["minimum"]:
                raise SchemaError(path, f"below minimum {s['minimum']}")
            if "maximum" in s and value > s["maximum"]:
                raise SchemaError(path, f"above maximum {s['maximum']}")
        if isinstance(value, str):
            if len(value) < s.get("minLength", 0):
                raise SchemaError(path, "too short")
            if "maxLength" in s and len(value) > s["maxLength"]:
                raise SchemaError(path, "too long")
            if "pattern" in s and not re.search(s["pattern"], value):
                raise SchemaError(path, f"does not match {s['pattern']!r}")
        if isinstance(value, list):
            if len(value) < s.get("minItems", 0):
                raise SchemaError(path, f"needs at least {s['minItems']} items")
            if "maxItems" in s and len(value) > s["maxItems"]:
                raise SchemaError(path, f"at most {s['maxItems']} items")
            if s.get("uniqueItems"):
                seen = [json.dumps(v, sort_keys=True) for v in value]
                if len(set(seen)) != len(seen):
                    raise SchemaError(path, "items must be unique")
            if "items" in s:
                for i, v in enumerate(value):
                    self._v(s["items"], v, f"{path}[{i}]", doc)
        if isinstance(value, dict):
            props = s.get("properties", {})
            for key in s.get("required", []):
                if key not in value:
                    raise SchemaError(f"{path}.{key}", "required")
            names = s.get("propertyNames")
            for key, v in value.items():
                if names is not None:
                    self._v(names, key, f"{path}.{key}", doc)
                if key in props:
                    self._v(props[key], v, f"{path}.{key}", doc)
                elif "additionalProperties" in s:
                    extra = s["additionalProperties"]
                    if extra is False:
                        raise SchemaError(f"{path}.{key}", "unknown field")
                    self._v(extra, v, f"{path}.{key}", doc)
        for sub in s.get("allOf", []):
            self._v(sub, value, path, doc)
        if "anyOf" in s and not any(self._ok(sub, value, path, doc) for sub in s["anyOf"]):
            raise SchemaError(path, "matches none of anyOf")
        if "oneOf" in s:
            n = sum(self._ok(sub, value, path, doc) for sub in s["oneOf"])
            if n != 1:
                raise SchemaError(path, f"must match exactly one of oneOf (matched {n})")
        if "not" in s and self._ok(s["not"], value, path, doc):
            raise SchemaError(path, "matches a forbidden schema")

    def _ok(self, s: Any, value: Any, path: str, doc: str) -> bool:
        try:
            self._v(s, value, path, doc)
        except SchemaError:
            return False
        return True


_REGISTRIES: dict[str, Registry] = {}


def registry(name: str = "ui_runtime") -> Registry:
    """The schemas shipped in `wakecore/protocol/schemas[/<name>]` ("kernel" = the top level; cached)."""
    if name not in _REGISTRIES:
        _REGISTRIES[name] = Registry.from_package("wakecore.protocol", "schemas" if name == "kernel" else f"schemas/{name}")
    return _REGISTRIES[name]
