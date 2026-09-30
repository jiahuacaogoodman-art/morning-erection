# Example plugin: `wakecore-example-plugin`

A minimal, working WakeCore plugin with one connector and one tool. Copy it as a template.

| Entry point | Group | Adapter id | What it does |
|---|---|---|---|
| `example_json_file` | `wakecore.connectors` | `example.json_file` | Watches records in a JSON file (snapshot, deterministic) |
| `example_journal` | `wakecore.tools` | `example.journal` | Appends one line per effect, idempotent by `effect_key`, reconcilable |

```bash
uv pip install -e examples/custom_connector
export WAKECORE_PLUGINS=example_json_file,example_journal   # nothing loads unless allowlisted
export WAKECORE_EXAMPLE_ROOT=/srv/wakecore-files            # both adapters stay inside this directory
uv run wakecore plugins list
```

Loading a plugin does not authorise it. The operator still has to allow its capabilities
(`files.read`, `files.append`) in `SystemPolicy`, and each task still needs a grant; the
planner only ever sees tools the task was granted.

Check your adapter against the kernel's contract before you publish it:

```python
from wakecore.testing.conformance import ConnectorScenario, ToolScenario, check_connector, check_tool

check_tool(lambda: JournalTool(root), ToolScenario(payload={"file": "j.log", "line": "hi"},
                                                   effect_count=lambda t: count_lines(root))).assert_ok()
```

See `tests/unit/test_plugins.py` and `tests/unit/test_conformance.py` in the WakeCore repository.
