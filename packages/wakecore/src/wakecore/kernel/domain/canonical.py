"""Deterministic JSON representation for digests (RFC §9.2, RFC 8785 subset).

Implements the JCS rules that matter for kernel payloads: lexicographic key order by
UTF-16 code units, no insignificant whitespace, minimal string escaping, and ES-style
number serialisation for integers and finite floats. Canonicalisation only unifies
representation; it never rewrites business content.
"""
import hashlib
import json
import math
from typing import Any

from .errors import SchemaMismatch


def _num(value: float) -> str:
    if not math.isfinite(value):
        raise SchemaMismatch("non-finite numbers cannot be canonicalised")
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))
    text = repr(value)
    if "e" in text:
        mantissa, exp = text.split("e")
        sign = "-" if exp.startswith("-") else "+"
        text = f"{mantissa}e{sign}{exp.lstrip('+-').lstrip('0') or '0'}"
    return text


def _encode(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, float):
        out.append(_num(value))
    elif isinstance(value, str):
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _encode(item, out)
        out.append("]")
    elif isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise SchemaMismatch("canonical JSON object keys must be strings")
        out.append("{")
        for i, key in enumerate(sorted(value, key=lambda k: k.encode("utf-16-be"))):
            if i:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _encode(value[key], out)
        out.append("}")
    else:
        raise SchemaMismatch(f"type {type(value).__name__} is not canonical JSON")


def canonical_json(value: Any) -> str:
    out: list[str] = []
    _encode(value, out)
    return "".join(out)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def short_hash(*parts: str, length: int = 24) -> str:
    """Deterministic identifier fragment derived from recorded inputs."""
    h = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return h[:length]
