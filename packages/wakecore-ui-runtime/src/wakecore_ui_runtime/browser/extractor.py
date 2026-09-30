"""Declarative extraction: DOM -> structured records, deterministic, no model.

Extractor config (part of the task's resource_scope, so it is digest-bound and replayable).
Exactly one of two modes:

  table  {"table": {"label": "可选课程"} | {"selector": "#table1"},   # aria-label/<caption>, or CSS
          "key_attr": "data-course-id" | "key_field": "email",       # row attribute, or a column
          "columns": [{"field": "seats", "headers": ["余量", "剩余名额"], "type": "int"}, ...]}
         Headers are matched by alias, so a column rename or reorder keeps working.

  list   {"list": {"item": ".inventory_item"},                      # one record per matching element
          "ready": ".inventory_list",                                # required: the page has rendered
          "key_attr": ... | "key_field": "name",
          "columns": [{"field": "name", "selector": ".inventory_item_name"},
                      {"field": "price", "selector": ".price", "attr": "data-cents", "type": "int"},
                      {"field": "in_cart", "selector": "button[data-test^=remove]", "type": "exists"}]}
         `selector` is relative to the item ("" = the item itself). Real sites are mostly cards
         and lists, not tables; an SPA may render after DOMContentLoaded, hence `ready`.

Common: "ready" (CSS, waited for before reading), "login": {"url_contains": [...],
"title_contains": [...], "selector": "#login-button"}.
Types: text | int | number (float; "1,234.5" accepted) | bool (with "true"/"false" word lists) |
exists (list mode: element present).

Per-column conversion (v1, all optional; not for `exists`), applied in this order to the raw text:
  "transform": "lower" | "upper"
  "pattern": "(\\d+) left", "group": 1     Python regex, *searched*; the value is the group (default 0)
  "type": ...                              then parsed as the type
  "default": <value>                       used only if declared: the cell is missing, the pattern
                                           does not match or the value does not parse
The key column (`key_field`) gets transform/pattern but no type and no default.

A missing table/column/cell or a value that no longer parses is SCHEMA_INVALID, never a
silently empty result (unless the column declares a `default`). innerText is used, so visually
hidden text (a favourite place for prompt injections) never becomes data. The browser only
returns raw strings; every conversion happens here, in Python, so it is testable without a page.

Check a file without opening a page:  python -m wakecore_ui_runtime.browser.extractor FILE.json
"""
import json
import re
import sys
from typing import Any, Optional

TYPES = ("text", "int", "number", "bool", "exists")
TRANSFORMS = ("lower", "upper")
COLUMN_OPTIONS = ("type", "true", "false", "transform", "pattern", "group", "default")
_INT = re.compile(r"^-?\d+$")
_NUMBER = re.compile(r"^-?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")
_CONVERSIONS = ("transform", "pattern", "group", "default")

EXTRACT_JS = """(cfg) => {
  const text = n => (n ? n.innerText : '').replace(/\\s+/g, ' ').trim();
  const keyOf = (el, cells) => cfg.key_attr ? el.getAttribute(cfg.key_attr) : cells[cfg.key_field];
  if (cfg.list) {
    const items = [...document.querySelectorAll(cfg.list.item)];
    const rows = items.map(it => {
      const cells = {};
      for (const col of cfg.columns) {
        const el = col.selector ? it.querySelector(col.selector) : it;
        if ((col.type || 'text') === 'exists') { cells[col.field] = el ? '1' : '0'; continue; }
        cells[col.field] = !el ? null : (col.attr ? el.getAttribute(col.attr) : text(el));
      }
      return {key: keyOf(it, cells), cells};
    });
    return {headers: [], rows};
  }
  const t = cfg.table;
  const table = t.selector ? document.querySelector(t.selector) : [...document.querySelectorAll('table')].find(x =>
      (x.getAttribute('aria-label') || '').trim() === t.label || (x.caption && text(x.caption) === t.label));
  if (!table || table.tagName !== 'TABLE') return {error: 'table_not_found'};
  const head = table.tHead ? table.tHead.rows[0] : table.rows[0];
  if (!head) return {error: 'header_not_found'};
  const headers = [...head.cells].map(text);
  const idx = {};
  for (const col of cfg.columns) {
    const i = headers.findIndex(h => col.headers.includes(h));
    if (i < 0) return {error: 'column_not_found:' + col.field, headers};
    idx[col.field] = i;
  }
  // without <thead> the header is rows[0], which the parser also puts in the implicit <tbody>
  const body = (table.tBodies.length ? [...table.tBodies].flatMap(b => [...b.rows]) : [...table.rows]).filter(r => r !== head);
  const rows = body.map(r => {
    const cells = {};
    for (const col of cfg.columns) cells[col.field] = r.cells[idx[col.field]] ? text(r.cells[idx[col.field]]) : null;
    return {key: keyOf(r, cells), cells};
  });
  return {headers, rows};
}"""


class ExtractorError(ValueError):
    pass


def _css(v: Any) -> bool:
    return isinstance(v, str) and 0 < len(v) <= 500


def validate(cfg: Any) -> dict[str, Any]:
    if not isinstance(cfg, dict) or set(cfg) - {"table", "list", "ready", "key_attr", "key_field", "columns", "login"}:
        raise ExtractorError("extractor: unknown or missing fields")
    if ("table" in cfg) == ("list" in cfg):
        raise ExtractorError("extractor needs exactly one of table / list")
    mode = "table" if "table" in cfg else "list"
    if mode == "table":
        t = cfg["table"]
        if not isinstance(t, dict) or len(t) != 1 or not (isinstance(t.get("label"), str) or _css(t.get("selector"))):
            raise ExtractorError("extractor.table needs exactly one of label / selector")
    else:
        lst = cfg["list"]
        if not isinstance(lst, dict) or set(lst) != {"item"} or not _css(lst["item"]):
            raise ExtractorError("extractor.list.item must be a CSS selector")
        if not _css(cfg.get("ready")):
            raise ExtractorError("extractor.ready is required in list mode (an empty list must be provable)")
    if "ready" in cfg and not _css(cfg["ready"]):
        raise ExtractorError("extractor.ready must be a CSS selector")
    if bool(cfg.get("key_attr")) == bool(cfg.get("key_field")):
        raise ExtractorError("extractor needs exactly one of key_attr / key_field")
    cols = cfg.get("columns")
    if not isinstance(cols, list) or not cols or len(cols) > 32:
        raise ExtractorError("extractor.columns must be a non-empty list")
    fields = set()
    for c in cols:
        if not isinstance(c, dict) or not isinstance(c.get("field"), str) or c["field"] in fields:
            raise ExtractorError("extractor column needs a unique field")
        fields.add(c["field"])
        if c.get("type", "text") not in TYPES:
            raise ExtractorError(f"extractor column {c['field']}: type must be one of {TYPES}")
        if mode == "table":
            if set(c) - {"field", "headers", *COLUMN_OPTIONS}:
                raise ExtractorError(f"extractor column {c['field']}: unknown fields for table mode")
            if not isinstance(c.get("headers"), list) or not c["headers"] or \
                    not all(isinstance(h, str) for h in c["headers"]):
                raise ExtractorError(f"extractor column {c['field']}: headers must be a list of strings")
            if c.get("type") == "exists":
                raise ExtractorError(f"extractor column {c['field']}: exists is a list-mode type")
        else:
            if set(c) - {"field", "selector", "attr", *COLUMN_OPTIONS}:
                raise ExtractorError(f"extractor column {c['field']}: unknown fields for list mode")
            if not isinstance(c.get("selector"), str) or len(c["selector"]) > 500:
                raise ExtractorError(f"extractor column {c['field']}: selector must be a string ('' = the item)")
            if "attr" in c and not (isinstance(c["attr"], str) and c["attr"]):
                raise ExtractorError(f"extractor column {c['field']}: attr must be an attribute name")
        _check_conversion(c, is_key=c["field"] == cfg.get("key_field"))
    if cfg.get("key_field") and cfg["key_field"] not in fields:
        raise ExtractorError("extractor.key_field must be one of the columns")
    login = cfg.get("login", {})
    if not isinstance(login, dict) or set(login) - {"url_contains", "title_contains", "selector"} or \
            ("selector" in login and not _css(login["selector"])):
        raise ExtractorError("extractor.login: url_contains / title_contains lists and an optional selector")
    return cfg


def _check_conversion(c: dict[str, Any], *, is_key: bool) -> None:
    f, kind = c["field"], c.get("type", "text")
    if kind == "exists" and set(c) & set(_CONVERSIONS):
        raise ExtractorError(f"extractor column {f}: exists takes no transform / pattern / group / default")
    if "transform" in c and c["transform"] not in TRANSFORMS:
        raise ExtractorError(f"extractor column {f}: transform must be one of {TRANSFORMS}")
    if "pattern" in c:
        if not isinstance(c["pattern"], str) or not 0 < len(c["pattern"]) <= 500:
            raise ExtractorError(f"extractor column {f}: pattern must be a regex string")
        try:
            rx = re.compile(c["pattern"])
        except re.error as e:
            raise ExtractorError(f"extractor column {f}: pattern does not compile ({e})") from None
        group = c.get("group", 0)
        if not isinstance(group, int) or isinstance(group, bool) or not 0 <= group <= rx.groups:
            raise ExtractorError(f"extractor column {f}: group must be 0..{rx.groups}")
    elif "group" in c:
        raise ExtractorError(f"extractor column {f}: group needs a pattern")
    for words in ("true", "false"):
        if words in c and not (isinstance(c[words], list) and all(isinstance(w, str) for w in c[words])):
            raise ExtractorError(f"extractor column {f}: {words} must be a list of strings")
    if "default" in c:
        if is_key:
            raise ExtractorError(f"extractor column {f}: the key column cannot have a default")
        d = c["default"]
        ok = d is None or {"text": isinstance(d, str), "bool": isinstance(d, bool),
                           "int": isinstance(d, int) and not isinstance(d, bool),
                           "number": isinstance(d, (int, float)) and not isinstance(d, bool)}[kind]
        if not ok:
            raise ExtractorError(f"extractor column {f}: default must be null or a {kind}")


_MISSING = object()


def _text(col: dict[str, Any], v: Optional[str]) -> Any:
    """transform + pattern; _MISSING when there is no value to parse."""
    if v is None:
        return _MISSING
    if col.get("transform") == "lower":
        v = v.lower()
    elif col.get("transform") == "upper":
        v = v.upper()
    if "pattern" in col:
        m = re.search(col["pattern"], v)
        if m is None:
            return _MISSING
        v = m.group(col.get("group", 0)) or ""
    return v


def _cell(col: dict[str, Any], v: Optional[str]) -> Any:
    f, kind = col["field"], col.get("type", "text")
    if kind == "exists":
        if v is None:
            raise ExtractorError(f"cell_missing:{f}")
        return v == "1"
    t = _text(col, v)
    if t is _MISSING:
        if "default" in col:
            return col["default"]
        raise ExtractorError(f"cell_missing:{f}" if v is None else f"pattern_no_match:{f}")
    if kind == "int":
        if _INT.match(t):
            return int(t)
        err = f"not_an_int:{f}"
    elif kind == "number":
        if _NUMBER.match(t):
            return float(t.replace(",", ""))
        err = f"not_a_number:{f}"
    elif kind == "bool":
        if t in col.get("true", []):
            return True
        if t in col.get("false", []) or ("false" not in col and t not in col.get("true", [])):
            return False
        err = f"not_a_bool:{f}"
    else:
        return t
    if "default" in col:
        return col["default"]
    raise ExtractorError(err)


def convert(cfg: dict[str, Any], raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if raw.get("error"):
        raise ExtractorError(raw["error"])
    cols = {c["field"]: c for c in cfg["columns"]}
    key_field = cfg.get("key_field")
    records: dict[str, dict[str, Any]] = {}
    for row in raw["rows"]:
        key = row.get("key")
        if key_field and key is not None:
            key = _text(cols[key_field], key)
            key = None if key is _MISSING else key
        if not key or key in records:
            raise ExtractorError("row_key_missing_or_duplicate")
        records[key] = {f: _cell(cols[f], v) for f, v in row["cells"].items() if f != key_field}
    return records


def describe() -> dict[str, Any]:
    """For GET /v1/capabilities."""
    return {"modes": ["table", "list"], "types": list(TYPES), "transforms": list(TRANSFORMS),
            "column_options": list(COLUMN_OPTIONS)}


def is_login(cfg: dict[str, Any], url: str, title: str, page: Any = None) -> bool:
    login = cfg.get("login") or {}
    if any(m in url for m in login.get("url_contains", [])) or any(m in title for m in login.get("title_contains", [])):
        return True
    if page is not None and login.get("selector"):
        try:
            return page.query_selector(login["selector"]) is not None
        except Exception:  # noqa: BLE001 - mid-navigation: not provably a login page
            return False
    return False


def ready_selector(cfg: dict[str, Any]) -> str:
    """What to wait for before reading: the content, or the login wall, whichever comes first."""
    parts = [p for p in (cfg.get("ready"), (cfg.get("login") or {}).get("selector")) if p]
    return ", ".join(parts)


def main(argv: Optional[list[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m wakecore_ui_runtime.browser.extractor FILE.json", file=sys.stderr)
        return 2
    try:
        with open(args[0], encoding="utf-8") as f:
            cfg = json.load(f)
        validate(cfg)
    except (OSError, ValueError) as e:     # ExtractorError and JSONDecodeError are ValueErrors
        print(f"invalid: {e}", file=sys.stderr)
        return 1
    print(f"ok: {'table' if 'table' in cfg else 'list'} mode, {len(cfg['columns'])} columns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
