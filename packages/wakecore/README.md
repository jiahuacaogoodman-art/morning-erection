# WakeCore kernel

The WakeCore kernel: a durable runtime for long-running, permissioned autonomous tasks
(watch → decide → approve → act → verify → reconcile). Pure standard library; PostgreSQL
support via the `postgres` extra, the browser sidecar via the `ui` extra.

```bash
pip install morning-erection            # kernel only (SQLite dev store)
pip install "morning-erection[postgres]"
pip install "morning-erection[ui]"      # + the UI Runtime sidecar (Playwright)
wakecore demo
```

The import name is `wakecore`. See the project README and `docs/` for the architecture,
the UI Runtime protocol and the adapter interfaces.
