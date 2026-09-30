# WakeCore UI Runtime

The browser sidecar for WakeCore: Playwright is the *eye* (deterministic extraction, no model
call), OpenAI Computer Use is the *hand* (only for interactive operations). It owns browser
profiles, cookies and logins so neither the kernel nor the model ever sees them, and speaks
the UI Runtime protocol v1 over local HTTP.

```bash
pip install morning-erection-ui-runtime
playwright install chromium
wakecore-ui-runtime --state ~/.wakecore-ui --port 8765 --token "$WAKECORE_UI_RUNTIME_TOKEN"
wakecore-ui-login --state ~/.wakecore-ui --session my_site --url https://example.com/login
```

The import name is `wakecore_ui_runtime`. The protocol is specified in
`docs/spec/ui-runtime-protocol.md` and `spec/ui-runtime/openapi.v1.json`; any implementation
(e.g. a TypeScript one) that passes the conformance tests can replace this one.
