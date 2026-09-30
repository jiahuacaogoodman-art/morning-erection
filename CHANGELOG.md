# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may break the Python API;
the UI Runtime protocol only breaks on a new major, `wakecore.ui-runtime/2`).

## [Unreleased]

### Added
- `examples/web_watch_todomvc/`: grant, binding, task spec and extractor for a watch over the
  management API, with the timeline of a real run against the live demo site;
  `tests/unit/test_examples.py` keeps the files valid.
- **OpenAI-compatible model endpoints** (relays, proxies, self-hosted gateways speaking the
  Responses API): `--openai-base-url` is validated (`https`, or `http` on loopback only; no
  credentials, query or fragment), and the endpoint gets its own egress,
  `model:openai-compatible:<host>`, so grants and approvals name the recipient of the
  screenshots. The kernel takes it from `WAKECORE_UI_MODEL_EGRESS`; `browser.cua`'s
  `required_egress` and the default system policy follow it.
- Protocol (additive, `wakecore.ui-runtime/1`): `model_endpoint` and `model_egress` in
  `health` and `capabilities.act`; optional `model_egress` on `POST /v1/act`; new error
  `409 model_egress_mismatch`, answered before any journal record or model call (a journalled
  key is still answered from the journal). The kernel maps it to `FAILED_NO_EFFECT`.
- `wakecore-ui-runtime --check-model`: two short Responses calls. The model must click with
  the computer tool (only the tool can click), then name the colour of the screenshot returned
  on `previous_response_id`. It catches relays that answer 200 but drop `tools`
  (`computer_tool_unused`) or images (`image_not_seen`), stateless relays, missing models and
  Chat-Completions-only endpoints, with a hint for each. It never prints the key.
- `wakecore ui health` reports whether the kernel's model egress matches the sidecar's.
- **Endpoints without the hosted computer tool.** `--variant function` offers the same actions
  as a plain function tool `computer_actions`. It is for relays backed by Codex / ChatGPT
  accounts, which answer 400 `Unsupported tool type: computer`. The actions are executed by the
  same loop and guards. `--history auto|server|client`: when an endpoint refuses
  `previous_response_id`, the runtime resends the conversation instead, keeping only the last 3
  screenshots. `capabilities.act` reports `history`, and `variant` may be `function`. The fake
  model endpoint simulates both, and `tests/ui` runs a full act, the credential-field guard and
  the origin-escape guard through it.
- `tests/real`: R0 endpoint check; the base URL comes from `.secrets/openai.base_url` or
  `WAKECORE_REAL_BASE_URL`, plus `WAKECORE_REAL_VARIANT`. First real Computer Use run
  (2026-09-30), through a relay with `--variant function`: R0, R2 (TodoMVC) and R3 (saucedemo)
  passed. Each act was confirmed by a fresh observation, used 2 model calls, and replayed with
  no mismatches.
- **macOS desktop eye and hand** ([ADR 0008](docs/adr/0008-desktop-runtime.md)). A new
  sidecar, `wakecore-desktop-runtime` (protocol `wakecore.desktop-runtime/1`,
  [spec](docs/spec/desktop-runtime-protocol.md), [OpenAPI](spec/desktop-runtime/openapi.v1.json),
  schemas under `protocol/schemas/desktop_runtime/`), drives native apps through a Computer
  Use MCP bridge ([tmustier/codex-computer-use-mcp](https://github.com/tmustier/codex-computer-use-mcp)).
  - The eye (`observe`, `verify`) reads full accessibility trees and applies a declarative
    extractor that selects by `id`, `description`, `value`, `help`, `head`, `head_regex`,
    `under` and `nth`, never by index. An ambiguous match is an error.
  - The hand (`act`) is a guarded model loop over one plain function tool, `desktop_actions`
    (action space `wakecore.desktop-ax/1`). Actions name elements by index; there are no
    coordinates. It refuses credential fields, system-wide shortcuts and stale or disabled
    elements. Every action is journalled before it is sent, with its kind and index only,
    never the typed text.
  - Scope is a list of bundle IDs (`app ∈ apps`), bound into the action digest. Every tree is
    checked to come from the requested app (`app_mismatch`).
  - Kernel adapters (stdlib only): connector `desktop.macos` (`desktop.observe`) and tool
    `desktop.cua` (`desktop.operate`, `EXTERNAL_WRITE`). A tool is only `confirmed` by a fresh
    deterministic read. Otherwise the result is `failed_no_effect` when nothing mutating was
    sent, and `unknown` in every other case. Reconcile only re-observes. They are registered
    when `WAKECORE_DESKTOP_RUNTIME_URL` is set; the egress is
    `WAKECORE_DESKTOP_MODEL_EGRESS`.
  - `tests/contract/test_desktop_runtime.py` and `test_desktop_acceptance.py` use a fake
    bridge. `tests/real_desktop` (opt-in, `WAKECORE_REAL_DESKTOP=1`) was run for real on
    2026-09-30 against the macOS Calculator:
    - D1: the eye only, no key.
    - D2: a human stand-in enters 12, and the kernel approves an act. A real model then
      presses × 3 = through a relay, the value 36 is verified, and the task completes.

### Changed
- `--history auto` now retries **any** 400 on a chained turn once with client-side history,
  not only errors that mention `previous_response_id`. A relay was seen answering a bare
  `invalid_request` to chaining. Client-side history becomes sticky only after the retry
  succeeds.
- The sidecar HTTP layer (`make_handler`) takes the dispatcher, protocol name and server name
  per runtime, so the browser and desktop runtimes share one implementation of the error
  envelope, authentication and protocol header.

### Documentation
- Known limits now say plainly that no production planner model ships (the built-in reasoning
  adapter is scripted) and that no API lists inbox messages yet; both are on the roadmap.

## [0.3.0] - 2026-09-29

First public layout. Two distributions from one uv workspace:
`morning-erection` (import `wakecore`, the kernel, standard library only) and
`morning-erection-ui-runtime` (import `wakecore_ui_runtime`, the browser sidecar).

### Added
- **Browser eye and hand** (V0.3, [ADR 0006](docs/adr/0006-browser-eye-and-hand.md)): the
  `web.playwright` connector (deterministic extraction, no model) and the `browser.cua` tool
  (OpenAI Computer Use behind approval, write-ahead journal, send-once, post-verification),
  both talking to the sidecar over localhost HTTP.
- **UI Runtime protocol v1** (`wakecore.ui-runtime/1`): JSON Schemas for every request and
  response, validated server-side; `GET /v1/capabilities`; `POST /v1/extractors/validate`;
  `GET /v1/acts/{key}` (full journal record); `POST /v1/acts/{key}/cancel` (cooperative);
  `GET /v1/sessions`, `GET /v1/sessions/{ref}`, `POST /v1/sessions/{ref}/release`;
  `protocol`/`runtime_version` in health; the `WakeCore-Protocol` header; optional
  `timeouts`. The same act key with a different request is refused with 409
  `idempotency_key_reused`. The client negotiates the protocol on first use; a major-version
  mismatch makes the source `UNAVAILABLE ui_runtime_protocol_mismatch`.
- One error envelope for the sidecar, `{"error": {"code", "message", "retryable", "details"?}}`.
- Extractor v1 options: per-column `pattern`/`group` (regex), `type: number`,
  `transform: lower|upper`, explicit `default`. Existing configurations keep their digest.
- `wakecore ui check-extractor|health|capabilities|sessions` CLI commands.
- **Plugins**: entry-point groups `wakecore.connectors` and `wakecore.tools`, loaded only when
  allowlisted in `WAKECORE_PLUGINS`; bad plugins are isolated and cannot replace built-ins.
  `wakecore plugins list`. Example package in `examples/custom_connector`.
- **Conformance suite** `wakecore.testing.conformance` (`check_tool`, `check_connector`):
  descriptor consistency, legal statuses, send-once under repeated execute, reconcile never
  executes, honest failure reporting, secret handling. Every built-in adapter passes it.
- **MCP mapping** `wakecore.kernel.ports.annotations.to_mcp_tool`: MCP tool definitions with
  `readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint` and WakeCore guarantees in
  `_meta["dev.wakecore/…"]`.
- Management API: `GET /v1/tools`, `GET /v1/connectors`.
- **OpenAPI 3.1** documents generated from the live route tables: `spec/ui-runtime/openapi.v1.json`,
  `spec/kernel-api/openapi.v1.json` (`wakecore openapi`, `wakecore-ui-runtime --print-openapi`,
  `scripts/gen_openapi.py [--check]`), with a drift test.
- Real-environment test harness (`tests/real`, opt-in) against public test websites.
- Open-source project files: MIT licence, security policy, contributing guide (EN/中文),
  code of conduct, CI, issue forms.

### Changed
- Source moved to `packages/*/src`. `ui_runtime` → `wakecore_ui_runtime`;
  `simulators` → `wakecore_ui_runtime.testing`; `migrations/postgres` →
  `wakecore/adapters/postgres/sql`; `schemas/` → `wakecore/protocol/schemas`;
  the demo spec ships as package data.
- Console scripts `wakecore-ui-runtime` and `wakecore-ui-login` replace
  `python -m ui_runtime.server` / `python -m ui_runtime.login`.
- Development uses one uv-managed `.venv` (`uv sync --all-packages --all-extras --group dev`).
- Lint with ruff (E, F, I, W); the codebase is clean.

### Fixed
- Table-mode extraction returned the header row as a data record on tables without
  `<thead>` (the HTML parser puts that row into the implicit `<tbody>`); the header row is now
  always excluded. Found on a real public site.

## [0.2.0]

Kernel implementation of RFC WK-KERNEL-002 v0.2: task specs and versions, scheduling and
catch-up, signed ingress, deterministic change detection, decision profiles, effective
authority, approvals bound to the payload digest, budgets, leases and execution slots,
the `UNKNOWN`/reconcile state machine, crash recovery, read-only replay, SQLite (dev) and
PostgreSQL stores, the M2 concurrency gate. Not released to PyPI.

[Unreleased]: https://github.com/jiahuacaogoodman-art/morning-erection/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/jiahuacaogoodman-art/morning-erection/releases/tag/v0.3.0
