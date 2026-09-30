# Desktop Runtime Protocol v1 (`wakecore.desktop-runtime/1`)

Status: draft, implemented by `wakecore_ui_runtime.desktop` (unreleased, after 0.3.0).
Machine-readable: [spec/desktop-runtime/openapi.v1.json](../../spec/desktop-runtime/openapi.v1.json),
schemas in `packages/wakecore/src/wakecore/protocol/schemas/desktop_runtime/`.

The key words MUST, MUST NOT, SHOULD and MAY are to be read as in RFC 2119.

This is the native-app counterpart of the [UI Runtime protocol](ui-runtime-protocol.md). It has
the same transport, authentication, error envelope, idempotency and send-once semantics. Only the
differences are specified in detail here.

## 1. Model

- An **app** is a macOS application named by its **bundle ID** (`com.apple.calculator`). Display
  names are never accepted, because they are localised and ambiguous.
- Every request that touches an app carries `app` and `apps`. `apps` is the allow-list covered
  by the kernel's grant, and `app` MUST be a member of it (`403 app_outside_scope`).
- The runtime reaches apps through a **Computer Use MCP bridge** (reference:
  [tmustier/codex-computer-use-mcp](https://github.com/tmustier/codex-computer-use-mcp)). The
  bridge has no permission model of its own, so the runtime MUST check every call before
  sending it to the bridge.
- **The eye** (`observe`, `verify`) reads the accessibility tree and applies a declarative
  extractor. It MUST NOT call a model and MUST NOT send any action to the app.
- **The hand** (`act`) is a guarded model loop over one app. Actions name elements of the
  latest tree by index; there are no pixel coordinates and no drags (`action_space`
  `wakecore.desktop-ax/1`).

## 2. Transport, authentication, versioning

Same as UI Runtime v1 §2:
- The runtime listens on loopback only.
- Requests carry `Authorization: Bearer <token>` when the runtime was started with a token.
- Every response carries `WakeCore-Protocol: wakecore.desktop-runtime/1`.
- A client MUST refuse a runtime whose protocol family or major version differs from its own.
  In particular, a browser client MUST refuse a desktop runtime, and a desktop client MUST
  refuse a browser runtime. The kernel reports this as `UNAVAILABLE
  desktop_runtime_protocol_mismatch`.

## 3. Endpoints

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/v1/health` | – | `protocol`, `runtime_version`, `bridge{ok, server, server_version, detail}`, `model_endpoint`, `model_egress`. Local paths MUST NOT appear. |
| GET | `/v1/capabilities` | – | extractor modes/types, `act{action_space, guards, cancel, model_configured, model_egress, history}`, `endpoints` |
| POST | `/v1/extractors/validate` | `{extractor}` | no app is read |
| POST | `/v1/observe` | `{app, apps, extractor}` | `{outcome, records, content_digest, error_code, app, window}` |
| POST | `/v1/verify` | `{app, apps, extractor, record_id}` | `{present, record, ...}` |
| POST | `/v1/act` | `{key, attempt?, app, apps, goal, timeout_s?, max_steps?, model_egress?}` | see §5 |
| GET | `/v1/acts/{key}` | – | journal record: status, attempt, times, `mutating[]` (kind and element index only, never typed text), `blocked[]`, usage, artifacts |
| POST | `/v1/acts/{key}/cancel` | – | cooperative, checked between actions |
| GET | `/v1/act/status?key=` | – | `never_received` \| `in_progress` \| `finished` \| `interrupted`, with `mutating_actions` |

Observation outcomes:
- `SUCCESS`
- `AUTH_REQUIRED` (`login_screen`)
- `SCHEMA_INVALID` (`not_ready`, `extractor_invalid:*`, a missing field)
- `UNAVAILABLE` (`app_state_failed`, `app_mismatch`, `mcp_unavailable`, ...)

## 4. Reading

- The runtime MUST request full trees (`disableDiff: true`) and MUST NOT parse a diff.
- The tree MUST come from the requested bundle ID. Otherwise the result is `UNAVAILABLE
  app_mismatch`, and nothing else in the answer is used.
- Extractors find elements by what they are, never by index: `id`, `description`, `value`,
  `help`, `head`, `head_regex`, `under`, `nth`. A selector that matches more than one element
  without `nth` is an error, not "the first one".
- An extractor has a required `ready` selector, which proves the right window is showing. It
  has an optional `login` selector (→ `AUTH_REQUIRED`) and an optional `window` title check.
- Column conversion rules are exactly those of the browser extractor.
- Values of credential-looking elements are replaced before anything leaves the runtime.
  "Credential-looking" means password, secure text, one-time code and PIN, in several
  languages.

## 5. Acting

- **Scope.** `app ∈ apps`. `apps` and `app` are bound into the kernel's action digest and
  approval. The model never names an app.
- **Idempotency.** `key` is the kernel's effect key.
  - A second request with the same key and the same `app`/`apps`/`goal` MUST be answered from
    the journal (`deduplicated: true`). This holds even for a new `attempt`.
  - A different request under a used key MUST be refused with `409 idempotency_key_reused`.
- **Egress.** If `model_egress` is present and differs from the runtime's, the runtime MUST
  answer `409 model_egress_mismatch` before journalling anything or calling a model.
- **Write-ahead journal.** Each action MUST be journalled before it is sent. The journal
  records the kind and the element index, never the typed text or value.
- **Guards.** A refused action is not sent: the model is told and the rest of its batch is
  dropped. The runtime MUST refuse:
  - element indices that are not in the latest tree;
  - disabled elements;
  - `set_value` / `select_text` into credential fields, and typing or non-harmless keys while
    a credential field is on screen (→ `needs_human credential_field`);
  - system-wide key combinations (app switcher, Spotlight, force quit, lock, log out);
  - any tool other than the action function (→ `needs_human unsupported_action`).
- **Busy.** One desktop, one operation at a time. An observe that arrives during an act waits
  (bounded), then gets `503 desktop_busy` (retryable).
- **Result.**
  - `status`: `completed | incomplete | needs_human | model_error | canceled | refused | error`.
  - `reason`, `steps`, `actions_executed`, `mutating_actions`, `blocked`, `summary`, `usage`,
    `artifacts_ref`.
  - `completed` is only the model's claim. The caller MUST verify with a fresh `verify` before
    treating the effect as done.
- **Unknown effects.** A bridge timeout (`incomplete bridge_timeout`) or a restart (`interrupted`)
  may have delivered an action. The act MUST NOT be repeated: the kernel reconciles by
  re-observing.
- **Model history.** Turns are chained with `previous_response_id`. With `history=auto`, a 400
  on a chained turn is retried once with client-side history, and on success the runtime keeps
  sending the history. Some relays refuse chaining with a bare `invalid_request`.

## 6. Kernel mapping (reference adapters)

| Runtime answer | `desktop.cua` result |
|---|---|
| pre-verify already holds | `confirmed` (`already_satisfied`), nothing sent |
| post-verify holds | `confirmed` |
| not verified, `mutating_actions == 0` | `failed_no_effect` |
| not verified, `mutating_actions > 0`, transport failure, 5xx | `unknown` |
| 409 `model_egress_mismatch`, 4xx before anything was sent | `failed_no_effect` |

Reconcile never acts and never waits for the effect. It answers:
- `no_effect` when the journal says `never_received` or 0 mutating actions;
- `confirmed` when the condition holds;
- `still_unknown` otherwise.

## 7. Security requirements

- The runtime MUST NOT expose an endpoint that returns typed text, field values of credential
  elements, or screenshots over HTTP. Screenshots stay in the state directory.
- The API key MUST be read from a file that is not group- or world-readable, and only by the
  runtime process.
- The runtime MUST NOT forward MCP elicitation requests to the model.
