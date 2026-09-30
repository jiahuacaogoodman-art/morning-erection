"""Declarative extraction for desktop apps: accessibility tree -> records. Deterministic, no model.

The desktop counterpart of browser/extractor.py. Element indices change between sessions, so
elements are found by what they are, never by index:

  node selector  {"id": "StandardInputView"}                      exact matches, all ANDed
                 {"description": "等于"} {"value": "0"} {"help": "..."} {"head": "文本 3"}
                 {"head_regex": "^文本 "}                            Python regex, searched in the head
                 {"under": {<selector>}}                            must be inside a matching element
                 {"nth": 0}                                         pick one of several matches
  Without `nth`, a selector that matches more than one element is an error (ambiguous), never
  "the first one": an app update that duplicates an element must not silently change the data.

Exactly one of two modes, plus a required `ready` selector (proves the right window is showing):

  records  {"ready": {"id": "StandardInputView"},
            "records": [{"id": "display", "columns": [
                {"field": "value", "node": {"under": {"id": "StandardInputView"}, "head_regex": "."},
                 "pattern": "(\\S+)$", "group": 1}]}]}
           fixed records (a form, a status panel, a calculator display)

  list     {"ready": {...}, "list": {"item": {"under": {"id": "inbox"}, "head_regex": "^行"}},
            "key_field": "subject",
            "columns": [{"field": "subject", "node": {"description": "主题"}},
                        {"field": "unread", "node": {"description": "未读"}, "type": "exists"}]}
           one record per matching element; column selectors are searched inside the item
           (no `node` = the item itself)

Column: {"field", "node"?, "attr": "head"(default) | "value" | "description" | "help" | "id",
         "type", "transform", "pattern", "group", "default", "true", "false"} with exactly the
conversion rules of the browser extractor (text | int | number | bool | exists).
Optional "window": {"title": "...", "title_contains": "..."}: a different window is not ready.
Optional "login": <node selector>: if it matches, the app shows a sign-in screen (AUTH_REQUIRED).

Check a file:  python -m wakecore_ui_runtime.desktop.extractor FILE.json
"""
import json
import re
import sys
from typing import Any, Optional

from ..browser.extractor import COLUMN_OPTIONS, TRANSFORMS, TYPES, ExtractorError, _cell, _check_conversion
from .tree import AppState, Node

ATTRS = ("head", "value", "description", "help", "id")
SELECTOR_KEYS = ("id", "description", "value", "help", "head", "head_regex", "under", "nth")
_MATCH_KEYS = ("id", "description", "value", "help", "head", "head_regex")

__all__ = ["ExtractorError", "validate", "convert", "find", "find_all", "describe", "is_ready", "is_login"]


def _selector(sel: Any, where: str, depth: int = 0) -> None:
    if not isinstance(sel, dict) or not sel or set(sel) - set(SELECTOR_KEYS):
        raise ExtractorError(f"{where}: a node selector is an object with keys from {SELECTOR_KEYS}")
    if not any(k in sel for k in _MATCH_KEYS) and "under" not in sel:
        raise ExtractorError(f"{where}: a node selector needs at least one of {_MATCH_KEYS}")
    for k in ("id", "description", "value", "help", "head"):
        if k in sel and not (isinstance(sel[k], str) and len(sel[k]) <= 500):
            raise ExtractorError(f"{where}.{k} must be a string")
    if "head_regex" in sel:
        if not isinstance(sel["head_regex"], str) or not 0 < len(sel["head_regex"]) <= 500:
            raise ExtractorError(f"{where}.head_regex must be a regex string")
        try:
            re.compile(sel["head_regex"])
        except re.error as e:
            raise ExtractorError(f"{where}.head_regex does not compile ({e})") from None
    if "nth" in sel and not (isinstance(sel["nth"], int) and not isinstance(sel["nth"], bool) and
                             0 <= sel["nth"] <= 1000):
        raise ExtractorError(f"{where}.nth must be an integer 0..1000")
    if "under" in sel:
        if depth >= 4:
            raise ExtractorError(f"{where}: `under` nests at most 4 deep")
        _selector(sel["under"], f"{where}.under", depth + 1)


def validate(cfg: Any) -> dict[str, Any]:
    if not isinstance(cfg, dict) or set(cfg) - {"ready", "window", "login", "records", "list", "key_field", "columns"}:
        raise ExtractorError("extractor: unknown or missing fields")
    if ("records" in cfg) == ("list" in cfg):
        raise ExtractorError("extractor needs exactly one of records / list")
    if "ready" not in cfg:
        raise ExtractorError("extractor.ready is required (proves the right window is showing)")
    _selector(cfg["ready"], "extractor.ready")
    if "login" in cfg:
        _selector(cfg["login"], "extractor.login")
    win = cfg.get("window", {})
    if not isinstance(win, dict) or set(win) - {"title", "title_contains"} or \
            not all(isinstance(v, str) and v for v in win.values()):
        raise ExtractorError("extractor.window: {title?, title_contains?} strings")
    if "records" in cfg:
        if set(cfg) & {"key_field", "columns"}:
            raise ExtractorError("records mode takes its columns inside each record, and no key_field")
        recs = cfg["records"]
        if not isinstance(recs, list) or not 0 < len(recs) <= 64:
            raise ExtractorError("extractor.records must be a non-empty list")
        ids = set()
        for r in recs:
            if not isinstance(r, dict) or set(r) != {"id", "columns"} or not isinstance(r["id"], str) or \
                    not 0 < len(r["id"]) <= 200 or r["id"] in ids:
                raise ExtractorError("each record is {id, columns} with a unique id")
            ids.add(r["id"])
            _columns(r["columns"], f"record {r['id']}", key_field=None)
    else:
        lst = cfg["list"]
        if not isinstance(lst, dict) or set(lst) != {"item"}:
            raise ExtractorError("extractor.list is {item: <node selector>}")
        _selector(lst["item"], "extractor.list.item")
        if not isinstance(cfg.get("key_field"), str):
            raise ExtractorError("list mode needs key_field (one of the columns)")
        fields = _columns(cfg.get("columns"), "extractor", key_field=cfg["key_field"])
        if cfg["key_field"] not in fields:
            raise ExtractorError("extractor.key_field must be one of the columns")
    return cfg


def _columns(cols: Any, where: str, *, key_field: Optional[str]) -> set[str]:
    if not isinstance(cols, list) or not cols or len(cols) > 32:
        raise ExtractorError(f"{where}: columns must be a non-empty list")
    fields: set[str] = set()
    for c in cols:
        if not isinstance(c, dict) or not isinstance(c.get("field"), str) or not c["field"] or c["field"] in fields:
            raise ExtractorError(f"{where}: each column needs a unique field")
        fields.add(c["field"])
        if set(c) - {"field", "node", "attr", *COLUMN_OPTIONS}:
            raise ExtractorError(f"{where} column {c['field']}: unknown fields")
        if c.get("type", "text") not in TYPES:
            raise ExtractorError(f"{where} column {c['field']}: type must be one of {TYPES}")
        if c.get("attr", "head") not in ATTRS:
            raise ExtractorError(f"{where} column {c['field']}: attr must be one of {ATTRS}")
        if "node" in c:
            _selector(c["node"], f"{where} column {c['field']}.node")
        elif key_field is None:
            raise ExtractorError(f"{where} column {c['field']}: node is required in records mode")
        _check_conversion(c, is_key=c["field"] == key_field)
    return fields


# ------------------------------------------------------------------ matching

def _matches(state: AppState, n: Node, sel: dict[str, Any], scope: Optional[Node]) -> bool:
    for k in ("id", "description", "value", "help", "head"):
        if k in sel and n.attr(k) != sel[k]:
            return False
    if "head_regex" in sel and not re.search(sel["head_regex"], n.head):
        return False
    if "under" in sel:
        parents = [a for a in state.ancestors(n) if scope is None or a is scope or _inside(state, a, scope)]
        if not any(_matches(state, a, sel["under"], scope) for a in parents):
            return False
    return True


def _inside(state: AppState, n: Node, scope: Node) -> bool:
    return any(a is scope for a in state.ancestors(n))


def find_all(state: AppState, sel: dict[str, Any], scope: Optional[Node] = None) -> list[Node]:
    pool = state.nodes if scope is None else state.descendants(scope)
    return [n for n in pool if _matches(state, n, sel, scope)]


def find(state: AppState, sel: dict[str, Any], scope: Optional[Node] = None) -> Optional[Node]:
    """The one element a selector names; None if absent; ExtractorError if ambiguous."""
    hits = find_all(state, sel, scope)
    if "nth" in sel:
        return hits[sel["nth"]] if sel["nth"] < len(hits) else None
    if len(hits) > 1:
        raise ExtractorError(f"ambiguous_node:{len(hits)}_matches")
    return hits[0] if hits else None


def is_ready(cfg: dict[str, Any], state: AppState) -> bool:
    win = cfg.get("window") or {}
    title = state.window or ""
    if "title" in win and title != win["title"]:
        return False
    if "title_contains" in win and win["title_contains"] not in title:
        return False
    try:
        return find(state, cfg["ready"]) is not None
    except ExtractorError:
        return False


def is_login(cfg: dict[str, Any], state: AppState) -> bool:
    return "login" in cfg and bool(find_all(state, cfg["login"]))


def _raw(state: AppState, col: dict[str, Any], scope: Optional[Node]) -> Optional[str]:
    node = scope if "node" not in col else find(state, col["node"], scope)
    if col.get("type") == "exists":
        return "1" if node is not None else "0"
    if node is None:
        return None
    return node.attr(col.get("attr", "head"))


def convert(cfg: dict[str, Any], state: AppState) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if "records" in cfg:
        for r in cfg["records"]:
            records[r["id"]] = {c["field"]: _cell(c, _raw(state, c, None)) for c in r["columns"]}
        return records
    key_field = cfg["key_field"]
    cols = {c["field"]: c for c in cfg["columns"]}
    for item in find_all(state, cfg["list"]["item"]):
        key_col = cols[key_field]
        key = _cell({**key_col, "type": "text"}, _raw(state, key_col, item))
        if not key or key in records:
            raise ExtractorError("row_key_missing_or_duplicate")
        records[key] = {f: _cell(c, _raw(state, c, item)) for f, c in cols.items() if f != key_field}
    return records


def describe() -> dict[str, Any]:
    """For GET /v1/capabilities."""
    return {"modes": ["records", "list"], "types": list(TYPES), "transforms": list(TRANSFORMS),
            "column_options": list(COLUMN_OPTIONS), "attrs": list(ATTRS), "selector_keys": list(SELECTOR_KEYS)}


def main(argv: Optional[list[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m wakecore_ui_runtime.desktop.extractor FILE.json", file=sys.stderr)
        return 2
    try:
        with open(args[0], encoding="utf-8") as f:
            cfg = json.load(f)
        validate(cfg)
    except (OSError, ValueError) as e:
        print(f"invalid: {e}", file=sys.stderr)
        return 1
    print(f"ok: {'records' if 'records' in cfg else 'list'} mode")
    return 0


if __name__ == "__main__":
    sys.exit(main())
