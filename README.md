# WakeCore

[中文](README.zh-CN.md) · [Protocol](docs/spec/ui-runtime-protocol.md) · [Extractor](docs/extractor.md) ·
[Desktop protocol](docs/spec/desktop-runtime-protocol.md) ·
[Comparison](docs/comparison.md) · [ADRs](docs/adr) · [Changelog](CHANGELOG.md)

**A durable, permissioned runtime for long-running autonomous tasks.**
WakeCore watches sources on a schedule or on events, decides with deterministic rules whether
something is worth waking up for, calls a model only when it has to, and lets every external
action through only with a grant, an approval bound to the exact payload, a budget and an
idempotency key. It survives crashes, never repeats a side effect it cannot prove did not
happen, and can replay any task from its records without touching the world.

> Status: **alpha (0.3)**. The kernel's guarantees are covered by tests on SQLite and
> PostgreSQL; the browser eye and hand have been run against public test websites, and the
> macOS desktop eye and hand against the real Calculator, both with a real model through an
> OpenAI-compatible relay. See [Known limits](#known-limits).

## Why

Agents that run for days — "tell me when my grade is posted", "when a new todo appears, tick
it off", "watch this portal and file the form when the slot opens" — fail in boring ways:
they re-send an email after a crash, act on a page they were not allowed to touch, burn a
model call on every poll, or leak a cookie into a prompt. WakeCore is the part that makes
those failures structurally impossible, and leaves the model to do what only a model can.

```
            ┌───────────────────────── WakeCore kernel (stdlib only) ─────────────────────────┐
 schedule ─▶│ observe ─▶ detect change ─▶ decide ─▶ plan ─▶ authorise ─▶ approve ─▶ act ─▶ verify │
 ingress  ─▶│   │ deterministic      │ rules first     │ grant ∩ policy   │ digest-   │ send-  │ re-     │
 (HMAC)     │   │ no model           │ model if needed │ ∩ spec, origins  │ bound     │ once   │ observe │
            │   ▼                                                                    ▼            │
            │ evidence ── leases · slots · budgets · outbox · UNKNOWN → reconcile · replay ──────│
            └──────┬───────────────────────────────────────────────────────────────┬──────────┘
                   │ ObservationPort                                    ActionPort │
         connectors (plugins)                                        tools (plugins)
                   │                                                               │
                   └──────────────── UI Runtime sidecar (localhost HTTP) ──────────┘
                       eye  = Playwright, declarative extractor, no model call
                       hand = OpenAI Computer Use, write-ahead journal, origin allow-list
                       owns browser profiles: cookies and passwords never reach the kernel or the model
```

- **Kernel** (`wakecore`, PyPI `morning-erection`): pure Python standard library. Task specs and
  versions, triggers and catch-up, signed ingress, change detection, decision profiles,
  effective authority, approvals, budgets, leases and execution slots, crash recovery, the
  `UNKNOWN`/reconcile state machine, read-only replay. SQLite for development, PostgreSQL for
  production.
- **UI Runtime** (`wakecore_ui_runtime`, PyPI `morning-erection-ui-runtime`): a separate
  process speaking the [UI Runtime protocol v1](docs/spec/ui-runtime-protocol.md)
  ([OpenAPI](spec/ui-runtime/openapi.v1.json)). Any implementation that passes the protocol
  tests can replace it.

## Quick start

With [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/jiahuacaogoodman-art/morning-erection && cd morning-erection
uv sync --all-packages --all-extras --group dev
uv run wakecore demo            # offline: fake clock, scripted model, no external sends
uv run pytest -q --ignore=tests/ui
```

From PyPI:

```bash
pip install morning-erection                 # kernel only
pip install "morning-erection[postgres]"     # + psycopg
pip install "morning-erection[ui]"           # + the browser sidecar
```

Run the pieces:

```bash
export WAKECORE_SECRETS_FILE=~/.wakecore/secrets.json   # API tokens + secret refs, chmod 600; serve-api
                                                       # refuses to start without tokens (format: see the example)
wakecore --db sqlite:///wk.db init-db
wakecore --db sqlite:///wk.db serve-api --port 8080        # management + ingress API
wakecore --db sqlite:///wk.db worker                       # does the work

playwright install chromium
wakecore-ui-runtime --state ~/.wakecore-ui --port 8765 --token "$WAKECORE_UI_RUNTIME_TOKEN" \
    --openai-key-file ~/.config/wakecore/openai.key        # key only needed for Computer Use
    # --openai-base-url https://relay.example.com/v1      # optional: an OpenAI-compatible relay
wakecore-ui-runtime --check-model --openai-key-file ~/.config/wakecore/openai.key  # two tiny calls
wakecore-ui-login --state ~/.wakecore-ui --session my_site \
    --url https://example.com/login --done-url-contains /home   # you log in by hand, once
```

Then create a task (`POST /v1/tasks` with a [TaskSpec](packages/wakecore/src/wakecore/protocol/schemas/task_spec.v1.schema.json)),
confirm and activate it. The operator runbooks: [browser](docs/runbooks/browser.md),
[operations](docs/runbooks/operations.md), [PostgreSQL](docs/runbooks/postgres.md).
A complete browser-watch example is in [examples/web_watch_todomvc](examples/web_watch_todomvc).

**OpenAI-compatible endpoints.** The sidecar speaks the OpenAI Responses API. Any relay, proxy
or self-hosted gateway works if it implements that API with image input and either the
`computer` tool or plain function calling (`--variant function`, for relays that answer
`Unsupported tool type: computer`). Endpoints that refuse `previous_response_id` get the
conversation resent by the runtime. `--check-model` tests all of this. A relay sees the screenshots,
so it is a separate egress, `model:openai-compatible:<host>`, which you grant and set in the
kernel as `WAKECORE_UI_MODEL_EGRESS`. See the [browser runbook](docs/runbooks/browser.md) (§1.1).

**macOS desktop apps.** A second sidecar, `wakecore-desktop-runtime`, reads and operates native
apps through a Computer Use MCP bridge
([tmustier/codex-computer-use-mcp](https://github.com/tmustier/codex-computer-use-mcp)):

```bash
wakecore-desktop-runtime --state ~/.wakecore-desktop --port 8766 --token "$WAKECORE_DESKTOP_RUNTIME_TOKEN" \
    --mcp-command "/abs/path/to/node ~/src/codex-computer-use-mcp/dist/mcp-server.js" \
    --openai-key-file ~/.config/wakecore/openai.key
export WAKECORE_DESKTOP_RUNTIME_URL=http://127.0.0.1:8766   # the kernel registers desktop.macos + desktop.cua
```

The eye reads the accessibility tree with a declarative extractor and never calls a model.
The hand acts on element indices, not pixels. Both are scoped to the bundle IDs in the grant
(`resource_scope.apps`). Typing near a password or one-time-code field is refused and handed
to a human. See the [desktop runbook](docs/runbooks/desktop.md) (in Chinese) and
[ADR 0008](docs/adr/0008-desktop-runtime.md).

## What you get

| Guarantee | How |
|---|---|
| An effect happens at most once | `effect_key` per action, write-ahead journal, send-once in tools and the sidecar; a crash in the middle becomes `UNKNOWN`, resolved by re-observing, never by re-sending |
| Approvals mean something | An approval is bound to the action digest: payload, data egress and resource scope together. Change any of them and it needs a new approval |
| The model sees only what it may use | The planner gets the tools the task is granted, nothing else; credentials are references, resolved inside the adapter |
| Browser actions stay on their origins | `resource_scope.origins` is part of the digest; the sidecar blocks every other origin, popups, downloads and uploads |
| Desktop actions stay in their apps | `resource_scope.apps` (bundle IDs) is part of the digest; the desktop sidecar checks every tree and every action against it, and refuses credential fields and system-wide shortcuts |
| Watching is cheap and deterministic | High-frequency polling uses a declarative [extractor](docs/extractor.md) (no model call); the model is for decisions and unfamiliar interactions |
| Every write is verified | A tool reports `confirmed` only after re-observing the result |
| Replay is safe | `wakecore replay TASK` recomputes decisions from recorded evidence and calls no adapter |

## Extending

- **Connectors and tools** are plugins found through entry points (`wakecore.connectors`,
  `wakecore.tools`) and loaded only when allowlisted in `WAKECORE_PLUGINS`.
  Template: [examples/custom_connector](examples/custom_connector).
- **Conformance suite**: `wakecore.testing.conformance.check_tool / check_connector` checks an
  adapter against the kernel's contract (send-once, reconcile never executes, honest failures).
- **MCP**: `wakecore.kernel.ports.annotations.to_mcp_tool` exports any tool as an MCP tool
  definition with `readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint`.
- **APIs**: [management and ingress API](spec/kernel-api/openapi.v1.json),
  [UI Runtime](spec/ui-runtime/openapi.v1.json), [Desktop Runtime](spec/desktop-runtime/openapi.v1.json) — all generated from the route tables.

## How it compares

WakeCore overlaps with durable-execution engines (Temporal, Restate, DBOS, Inngest), agent
frameworks (LangGraph, OpenAI Agents SDK) and browser agents (browser-use, Stagehand, Skyvern,
Playwright MCP). It is narrower than each: it does not run arbitrary workflows or host
arbitrary agents. What it adds is the layer between "a model wants to click Submit" and the
click — authority, approval binding, send-once, verification and reconciliation — plus a
deterministic eye so that watching costs nothing. Details and sources:
[docs/comparison.md](docs/comparison.md).

## Repository layout

| Path | What |
|---|---|
| `packages/wakecore/src/wakecore/kernel` | domain, ports, scheduling, events, decision, policy, actions, execution, budget, commands, replay |
| `packages/wakecore/src/wakecore/adapters` | SQLite, PostgreSQL (+ SQL migrations), sources, tools, secrets, UI Runtime client |
| `packages/wakecore/src/wakecore/protocol` | JSON Schemas, schema validator, OpenAPI generator |
| `packages/wakecore/src/wakecore/app` | WSGI API, CLI, worker |
| `packages/wakecore-ui-runtime/src/wakecore_ui_runtime` | sidecar: sessions, observer, extractor, verifier, Computer Use loop, journal, request guard; `desktop/` is the macOS desktop sidecar (MCP bridge, tree extractor, guarded action loop); `testing/` has the simulated portal, simulated OpenAI and a fake bridge |
| `spec/` | generated OpenAPI documents |
| `tests/` | unit, contract, security, recovery (randomised faults), replay, PostgreSQL gate, ui, real (websites), real_desktop (macOS Calculator) |
| `docs/` | protocol spec, extractor reference, comparison, ADRs, runbooks, roadmap |

## Known limits

- SQLite tests prove logic, not concurrency; concurrency is proven by the PostgreSQL gate,
  run on a single-node PostgreSQL 16 with a non-superuser role. No multi-node, failover or
  capacity testing has been done.
- The real-model scenarios were run on 2026-09-30 through one OpenAI-compatible relay, not
  against api.openai.com: `tests/real` R2 (TodoMVC) and R3 (saucedemo) with
  `--variant function`, and `tests/real_desktop` D1/D2 on the macOS Calculator. These are
  single runs on demo targets, not a reliability measurement. The planner is scripted in
  every test.
- The desktop runtime is macOS-only, needs Node and the Accessibility / Screen Recording
  permissions, and does one operation at a time. Its credential-field detection is keyword
  based and errs towards handing control to a human.
- **No production planner model ships yet.** The built-in reasoning adapter is scripted, so a
  deployed task with `"on_met": "plan"` proposes no action. Watch-and-notify tasks
  (`"on_met": "notify"`) need no model and work end to end; see
  [examples/web_watch_todomvc](examples/web_watch_todomvc/), which includes a real run.
- Inbox notifications are stored and shown as confirmed actions in the timeline, but no API
  endpoint lists inbox messages yet.
- The eye has been run against public demo/test sites (the-internet, books.toscrape,
  TodoMVC, saucedemo), not against production business systems.
- Model judgement quality (recall, false positives, prompt injection resistance) is not
  evaluated. Passing state-machine tests says nothing about it.
- Side effects a website performs on GET are not in the sidecar's write-ahead journal and
  can only be found by re-observation.

See [docs/roadmap.md](docs/roadmap.md).

## Contributing and security

[CONTRIBUTING.md](CONTRIBUTING.md) (DCO sign-off, test layers, the rules that are not
negotiable) · [SECURITY.md](SECURITY.md) (private reporting, threat model) ·
[Code of conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE).
