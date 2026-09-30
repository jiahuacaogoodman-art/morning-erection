# Extractor reference (v1)

An **extractor** tells the UI Runtime's eye how to turn one web page into flat records, without
a model and without clicking anything. It is part of the task's `resource_scope`, so it is
authorised together with the task, bound into the digest, and replayable.

```json
{
  "origins": ["https://demo.playwright.dev"],
  "url": "https://demo.playwright.dev/todomvc/#/",
  "extractor": { "...": "this document" },
  "record_ids": ["交房租"],
  "missing_targets": "absent"
}
```

The same page state always produces the same records and the same `content_digest`
(SHA-256 of the records as sorted-key JSON). That is what makes change detection
deterministic and polling free of model calls.

Authoritative implementation:
[`wakecore_ui_runtime/browser/extractor.py`](../packages/wakecore-ui-runtime/src/wakecore_ui_runtime/browser/extractor.py).
Schema: [`extractor.v1.schema.json`](../packages/wakecore/src/wakecore/protocol/schemas/ui_runtime/extractor.v1.schema.json).

## Shape

Exactly one **mode** (`table` or `list`), exactly one **key** (`key_attr` or `key_field`),
1–32 **columns**, and optionally `ready` and `login`.

| Member | Mode | Meaning |
|---|---|---|
| `table.label` | table | a `<table>` whose `aria-label` or `<caption>` equals this text |
| `table.selector` | table | a CSS selector for the `<table>` (use one of `label` / `selector`) |
| `list.item` | list | CSS selector; one record per matching element |
| `ready` | both; **required** in list mode | CSS selector that proves the page has rendered. Waited for before reading; if it is absent the result is `SCHEMA_INVALID not_ready`, never an empty list |
| `key_attr` | both | take the record id from this attribute of the row / item element |
| `key_field` | both | take the record id from this column (the column is not repeated inside the record) |
| `columns[]` | both | see below |
| `login` | both | `{url_contains: [..], title_contains: [..], selector}` — markers of a login page. A match yields `AUTH_REQUIRED login_page`, so an expired session is never read as "no data" |

## Columns

| Member | Mode | Meaning |
|---|---|---|
| `field` | both | record field name, unique |
| `headers` | table | header texts that identify the column (aliases). Columns are found by header, so a renamed or reordered column keeps working if you list both names |
| `selector` | list | CSS selector relative to the item; `""` means the item itself |
| `attr` | list | read this attribute instead of the text |
| `type` | both | `text` (default), `int`, `number` (float; `1,234.5` accepted), `bool`, `exists` (list only: whether `selector` matches) |
| `true`, `false` | both | word lists for `bool`. With only `true`, everything else is `false` |
| `transform` | both | `lower` or `upper`, applied to the raw text first |
| `pattern`, `group` | both | Python regex, *searched* in the text; the value is group `group` (default 0) |
| `default` | both | used **only if declared**, when the cell is missing, the pattern does not match or the value does not parse. Must be `null` or a value of the column's type. Not allowed on the key column |

Conversion order for one cell: raw text → `transform` → `pattern`/`group` → parse as `type` →
`default` on failure. `exists` takes none of these options. The key column gets `transform` and
`pattern` but no `type` and no `default`.

Text is read with `innerText` and whitespace-collapsed. Visually hidden text — a favourite
place for prompt injections — therefore never becomes data.

In table mode the header row is `tHead.rows[0]` if the table has a `<thead>`, otherwise the
first row; it is never returned as a record.

## Strictness

A missing table, header, column or cell, a regex that stops matching, or a value that stops
parsing is `SCHEMA_INVALID` with a code that names the field (`column_not_found:due`,
`cell_missing:price`, `pattern_no_match:seats`, `not_an_int:seats`,
`row_key_missing_or_duplicate`, …) — never a silently empty result. The kernel treats
`SCHEMA_INVALID` as "the site changed shape", marks the source unhealthy and waits for a human;
it never concludes that the watched data is gone. Declare `default` only where "missing"
genuinely has a meaning.

## Targets that do not exist yet

By default a `record_ids` target that is not on the page is a **coverage gap**: the observation
is `PARTIAL` and nothing is concluded about it. For pages where "not on the page" is a real
state (a todo that has not been written, a listing that has not appeared), set
`"missing_targets": "absent"` in the resource scope. That requires `ready`, so "the page has
rendered" is checked rather than assumed; a record that appears later is then a real change.

## Examples from real public sites

These are the extractors the opt-in real-environment tests (`tests/real`) run against public
test websites.

**Table, found by selector** — [the-internet /tables](https://the-internet.herokuapp.com/tables),
`#table1` has no `<thead>`:

```json
{"table": {"selector": "#table1"}, "key_field": "email", "columns": [
  {"field": "last",  "headers": ["Last Name"]},
  {"field": "first", "headers": ["First Name"]},
  {"field": "email", "headers": ["Email"]},
  {"field": "due",   "headers": ["Due"]},
  {"field": "site",  "headers": ["Web Site"]}]}
```

→ `{"jsmith@gmail.com": {"last": "Smith", "first": "John", "due": "$50.00", "site": "http://www.jsmith.com"}, …}`

**List of cards, an attribute and a presence flag** — [books.toscrape.com](https://books.toscrape.com):

```json
{"list": {"item": "article.product_pod"}, "ready": "ol.row", "key_field": "title", "columns": [
  {"field": "title",    "selector": "h3 a", "attr": "title"},
  {"field": "price",    "selector": ".price_color"},
  {"field": "in_stock", "selector": ".instock.availability", "type": "exists"}]}
```

→ `{"A Light in the Attic": {"price": "£51.77", "in_stock": true}, …}` — twenty records per page;
two reads give the same `content_digest`.

**Client-side app** — [TodoMVC](https://demo.playwright.dev/todomvc/#/) renders after
`DOMContentLoaded`, hence `ready`:

```json
{"list": {"item": "li[data-testid=todo-item]"}, "ready": "input.new-todo", "key_field": "title",
 "columns": [
   {"field": "title", "selector": "label[data-testid=todo-title]"},
   {"field": "done",  "selector": "input.toggle:checked", "type": "exists"}]}
```

→ for example `{"交房租": {"done": false}, "买牛奶": {"done": true}}`.
A complete task built on it is in [examples/web_watch_todomvc](../examples/web_watch_todomvc).

**Behind a login** — [saucedemo](https://www.saucedemo.com) shows its login form at the same
URL when the session is gone; `login.selector` turns that into `AUTH_REQUIRED`:

```json
{"list": {"item": ".inventory_item"}, "ready": ".inventory_list", "key_field": "name",
 "columns": [
   {"field": "name",    "selector": ".inventory_item_name"},
   {"field": "price",   "selector": ".inventory_item_price"},
   {"field": "in_cart", "selector": "button[data-test^=remove]", "type": "exists"}],
 "login": {"selector": "#login-button"}}
```

**v1 conversions** — turning `"$29.99"` into a number and `"12 seats left"` into an int, with a
declared fallback:

```json
{"field": "price", "selector": ".price", "pattern": "[\\d,.]+", "type": "number"},
{"field": "seats", "headers": ["Seats", "余量"], "pattern": "(\\d+) seats left", "group": 1,
 "type": "int", "default": 0},
{"field": "status", "headers": ["Status"], "transform": "lower", "type": "bool", "true": ["open"]}
```

## Checking an extractor

Without opening a page:

```bash
wakecore ui check-extractor my_extractor.json          # schema check, offline
WAKECORE_UI_RUNTIME_URL=http://127.0.0.1:8765 wakecore ui check-extractor my_extractor.json
                                                       # the runtime's full check, returns the normalised form
python -m wakecore_ui_runtime.browser.extractor my_extractor.json
```

`check-extractor` also accepts a whole resource scope and checks its `extractor` member.
Against a live page, call `POST /v1/observe` once and look at `records` / `error_code`.

## Compatibility

Every v1 option is optional; an extractor written for v0.3 is valid unchanged and produces the
same records and the same digest. Nested records and multi-page crawls are out of scope for v1
(the kernel's conditions compare flat fields); see the [roadmap](roadmap.md).
