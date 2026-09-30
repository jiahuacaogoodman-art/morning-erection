# Examples

| Directory | What it shows | Needs |
|---|---|---|
| [`web_watch_todomvc/`](web_watch_todomvc/) | A real watch over the management API: grant → binding → task → activate. The eye reads a public page every minute, notifies once when a todo appears, and completes when it is ticked. Includes the timeline of a real run. | uv dev environment, Chromium, network |
| [`custom_connector/`](custom_connector/) | A plugin package with one connector and one tool, loaded through entry points and an allowlist, and checked with `wakecore.testing.conformance` | Nothing beyond the kernel |

The offline demo needs no example files: `uv run wakecore demo` runs the grades scenario on a
fake clock with a scripted model (`uv run wakecore demo --spec` prints its task spec).

`tests/unit/test_examples.py` keeps the JSON files here valid against the published schemas
and the kernel's parsers.
