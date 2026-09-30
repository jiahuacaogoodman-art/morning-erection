# WakeCore UI Runtime protocol, version 1

| | |
|---|---|
| Identifier | `wakecore.ui-runtime/1` |
| Status | Draft, implemented by `wakecore_ui_runtime` 0.3.0 |
| Machine-readable | [`spec/ui-runtime/openapi.v1.json`](../../spec/ui-runtime/openapi.v1.json) (generated), JSON Schemas in [`wakecore/protocol/schemas/ui_runtime`](../../packages/wakecore/src/wakecore/protocol/schemas/ui_runtime) |
| Clients | `wakecore.adapters.ui_runtime.client.UiRuntimeClient` (kernel side) |

The key words MUST, MUST NOT, REQUIRED, SHOULD, SHOULD NOT and MAY are to be interpreted as
described in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119) and
[RFC 8174](https://www.rfc-editor.org/rfc/rfc8174) when, and only when, they appear in capitals.

## 1. Purpose and roles

A **UI Runtime** is a process that owns web browsers on behalf of a WakeCore kernel. It offers
two capabilities:

- **the eye** — `observe` and `verify`: navigate to a URL inside an origin allow-list and turn
  the page into flat records with a declarative [extractor](../extractor.md). Deterministic;
  no model is called; nothing is clicked or typed.
- **the hand** — `act`: let a computer-use model operate one page towards a goal, under
  guards, with a durable write-ahead journal that makes the operation happen at most once
  per key.

The **kernel** decides *whether* and *what*; the runtime only does *how*. The runtime MUST NOT
make authorisation decisions beyond the scope the kernel sends in each request, and it MUST
NOT report an act as the kernel's success: a kernel-side tool verifies the result with a fresh
`verify` before anything is confirmed.

The runtime **owns browser profiles** (cookies, local storage, logins). Credentials MUST NOT
be accepted in, or returned from, any endpoint of this protocol. A human logs in to a profile
out of band (the reference implementation ships `wakecore-ui-login`).

## 2. Transport and authentication

- HTTP/1.1 with JSON bodies (`Content-Type: application/json`, UTF-8).
- The runtime MUST listen on a loopback address only (`127.0.0.1` / `::1`).
- If the runtime was started with a token, every request MUST carry
  `Authorization: Bearer <token>`; the runtime MUST compare it in constant time and answer
  `401 unauthorised` otherwise. Runtimes SHOULD be started with a token whenever other
  processes share the host.
- Request bodies are limited (`limits.max_body_bytes` in capabilities, 256 KiB in the
  reference implementation); larger bodies MUST be answered `413 payload_too_large`.
- Every request body MUST be a JSON object and MUST validate against the endpoint's request
  schema; objects are closed (`additionalProperties: false`) unless the schema says otherwise.
  Violations are `400 invalid_request` with `details.path` pointing at the offending member.

## 3. Versioning and negotiation

- Every response MUST carry the header `WakeCore-Protocol: wakecore.ui-runtime/1`, and
  `GET /v1/health` MUST return `protocol` with the same value.
- Within major version 1, changes are **additive only**: new endpoints, new optional request
  members, new response members, new error codes, new `reason` strings. Clients MUST ignore
  response members they do not know. The meaning of an existing member, status or code MUST
  NOT change.
- A client MUST call `GET /v1/health` once per connection before any other operation and MUST
  refuse to talk to a runtime whose protocol family differs or whose major version differs (or
  that returns no `protocol` at all). The kernel client raises `ProtocolMismatch`, which the
  connector reports as `UNAVAILABLE ui_runtime_protocol_mismatch` and the tool as
  `failed_no_effect` — in both cases before anything was sent.
- `GET /v1/capabilities` describes optional features (extractor types, whether a model is
  configured, cancel support, guards, limits). Clients SHOULD consult it instead of probing.

## 4. Errors

Every non-2xx response MUST have the body

```json
{"error": {"code": "idempotency_key_reused", "message": "…", "retryable": false, "details": {}}}
```

`code` is stable and machine-readable, `message` is for humans, `retryable` tells whether the
same request may succeed later, `details` is optional. Codes in v1:

| HTTP | code | retryable | meaning |
|---|---|---|---|
| 400 | `bad_body` | no | not JSON, not an object, bad Content-Length |
| 400 | `invalid_request` | no | the body does not match the request schema |
| 400 | `invalid_session_ref` | no | `session_ref` must match `^[A-Za-z0-9_.-]{1,64}$` |
| 401 | `unauthorised` | no | missing or wrong bearer token |
| 404 | `not_found` | no | no such endpoint |
| 404 | `act_not_found` | no | no act was ever received under this key |
| 404 | `session_not_found` | no | no profile and no browser for this `session_ref` |
| 405 | `method_not_allowed` | no | |
| 409 | `idempotency_key_reused` | no | the act key was used for a different request (§6.2) |
| 409 | `model_egress_mismatch` | no | the act was approved for a different model recipient (§6.5) |
| 413 | `payload_too_large` | no | |
| 500 | `internal` | yes | unexpected failure in the runtime |
| 504 | `runtime_timeout` | yes | the browser did not finish in time |

Clients MUST treat an unknown code by its HTTP status class. Page-level problems (login
wall, schema drift, navigation failure) are **not** HTTP errors: they are `outcome` values in
a `200` response (§5.1), because they are facts about the observed world.

For `POST /v1/act`, a client that receives a 5xx or loses the connection MUST NOT assume that
nothing happened; it MUST find out through `GET /v1/act/status` or `GET /v1/acts/{key}` (§6.4).

## 5. The eye

### 5.1 `POST /v1/observe`

Request: `session_ref`, `url`, `origins` (allow-list, each `scheme://host[:port]`),
`extractor`, optional `timeouts {navigation_s, ready_s}`.

The runtime MUST:

1. validate the extractor (invalid → `outcome: SCHEMA_INVALID`, `error_code: extractor_invalid:…`);
2. refuse a `url` outside `origins` (`UNAVAILABLE origin_outside_scope`) without navigating;
3. navigate in the profile named by `session_ref`, with every request, redirect and popup held
   to `origins`, and without clicking, typing or submitting anything;
4. classify the page and answer `200` with exactly one `outcome`:

| outcome | error_code examples | meaning for the kernel |
|---|---|---|
| `SUCCESS` | `null` | `records` is the complete target set; `content_digest` = SHA-256 of the canonical records |
| `AUTH_REQUIRED` | `login_page`, `redirected_off_origin`, `http_401`, `http_403` | a human has to log in again; the binding becomes `REAUTH_REQUIRED` |
| `SCHEMA_INVALID` | `extractor_invalid:…`, `not_ready`, `table_not_found`, `header_not_found`, `column_not_found:<field>`, `cell_missing:<field>`, `pattern_no_match:<field>`, `not_an_int:<field>`, `not_a_number:<field>`, `not_a_bool:<field>`, `row_key_missing_or_duplicate` | the page changed shape; never treated as "the data is gone" |
| `UNAVAILABLE` | `navigation_failed`, `http_5xx`, `origin_outside_scope` | transient or scope problem; retry later |

`records` is an object `record_id → {field: scalar}` and MUST be `{}` unless the outcome is
`SUCCESS`. The response also carries `url` and `title` of the page that was read, and `detail`
for diagnostics. Clients MUST NOT parse `error_code` beyond its prefix up to the first `:`. A runtime MUST NOT return partial records as `SUCCESS`. The same page state MUST
yield the same records and the same `content_digest`.

### 5.2 `POST /v1/verify`

Same as observe plus `record_id`. Answers `outcome`, `present` (the record exists),
`record` (its fields, or `null`) and `content_digest`. Used by the kernel after an act and
during reconciliation; it has the same no-side-effect guarantee as observe.

### 5.3 `POST /v1/extractors/validate`

`{extractor}` → `{ok: true, mode, normalized}` or `{ok: false, error: {code: "extractor_invalid", message}}`.
MUST NOT open a page. The normalised form is what the runtime would execute.

## 6. The hand

### 6.1 `POST /v1/act`

Request: `key` (REQUIRED; the kernel's `effect_key`), `attempt` (the kernel's attempt id),
`session_ref`, `goal` (natural language), `start_url`, `origins`, optional `login` markers,
`timeout_s`, `max_steps`, `model_egress` (§6.5).

The runtime MUST, in this order:

1. deduplicate by `key` (§6.2) before touching any page;
1a. if the act is not a duplicate, check `model_egress` (§6.5);
2. create a durable journal record `in_progress` for the key (fsync'd) before the first
   browser action;
3. hold every network request to `origins`, and append every mutating request
   (`POST`/`PUT`/`PATCH`/`DELETE`) to the journal **before** the browser is allowed to send it;
4. block a second identical submit within the same act;
5. refuse to type into password or one-time-code fields, never acknowledge a model's pending
   safety check, close popups, dismiss dialogs, leave file choosers unanswered, disable
   downloads (`capabilities.act.guards` lists the guards in force);
6. stop at `timeout_s` / `max_steps` / a cancel request (§6.3);
7. write the result to the journal and answer `200`.

Response `status` values:

| status | meaning | typical `reason` |
|---|---|---|
| `completed` | the model says the goal is reached (a claim, not a verification) | `model_finished` |
| `incomplete` | stopped before the goal | `max_steps`, `timeout`, `navigation_failed` |
| `needs_human` | the runtime stopped because a human must act | `needs_login`, `credential_field`, `origin_escape`, `safety_check:<codes>`, `bad_action`, `model_needs_human` |
| `refused` | the request itself was out of scope; nothing was done | `origin_outside_scope` |
| `model_error` | the model could not be used | `model_not_configured`, provider error code |
| `canceled` | a cancel request was honoured | `cancel_requested` |
| `error` | unexpected failure inside the loop | exception type |
| `in_progress` | only on a duplicate: another request for this key is running now | |
| `interrupted` | only on a duplicate: the runtime restarted while this key was running | `runtime_restarted` |

`mutating_requests` (REQUIRED) is the number of writes that may have reached the site. It is
the fact the kernel's reconciliation depends on: `0` means provably nothing was submitted.
Other members: `steps`, `actions_executed`, `blocked_requests`, `blocked`, `final_url`,
`summary`, `usage`, `model_calls`, `model_ref`, `error_detail`, `artifacts_ref`, `action_space`
(`wakecore.computer/1`), `deduplicated`, `journal_status`.

### 6.2 Idempotency and send-once

The runtime computes a request digest over `session_ref`, `goal`, `start_url`, sorted
`origins` and `login`. (`attempt`, `timeout_s` and `max_steps` may differ between retries of
the same effect.)

| Journal state for `key` | Request | Runtime MUST |
|---|---|---|
| none | any | run the act |
| any | different digest | answer `409 idempotency_key_reused` (with `details.journal_status`, `details.mutating_requests`) and do nothing |
| `in_progress` | same digest | answer `status: in_progress, deduplicated: true` without touching the page |
| `finished` / `interrupted` with `mutating_requests > 0` | same digest | return the journalled result with `deduplicated: true`; **never operate again** |
| `finished` / `interrupted` with `mutating_requests = 0` | same digest, **new** `attempt` | MAY run again (nothing was ever sent) |
| `finished` / `interrupted` with `mutating_requests = 0` | same digest, same or no `attempt` | return the journalled result |

On start-up the runtime MUST turn every `in_progress` record into `interrupted`; a duplicate of
such a key is answered `status: interrupted, reason: runtime_restarted` together with the
journalled `mutating_requests`. A runtime MUST NOT "repair" a key by re-running it.
Journal records written by v0.3 runtimes carry no digest and are never answered with 409.

### 6.3 `POST /v1/acts/{key}/cancel`

Cooperative. The runtime MUST honour a cancel between model turns and between the actions of
one batch, and MUST NOT interrupt an action half-way. Response: `{key, cancel_requested,
status}` with `status` one of `queued` (received, not started yet; it will end `canceled` before its first action),
`in_progress`, `finished`, `interrupted`; `cancel_requested: false` when the act had already ended. A cancelled act ends with
`status: canceled` and its `mutating_requests`; the kernel treats it exactly like a failure
(`0` → no effect, otherwise `UNKNOWN` and reconcile).

### 6.4 Looking an act up

- `GET /v1/act/status?key=` → `{status: never_received | in_progress | finished | interrupted,
  mutating_requests, attempt, result_status}`. `never_received` is a strong statement: no act
  was ever journalled under this key, so nothing was sent.
- `GET /v1/acts/{key}` → the full journal record: `status`, `attempt`, `started_at`,
  `finished_at`, `cancel_requested_at`, `request_digest`, `mutating` and `blocked` request
  logs, `result`, `history`, `artifacts {ref, screenshots, trace}`.

Request logs contain method, `scheme://host/path` (no query string), response status, reason
and time. They MUST NOT contain request bodies, headers or cookies. Artifact paths are relative
to the runtime's state directory; artifacts are served by no endpoint of this protocol.

### 6.5 Model endpoint and egress

The runtime sends screenshots to a model endpoint. That endpoint is a data recipient, so it
has a name, the **model egress**, that the kernel's grants and approvals bind:

- `model:openai-computer-use` when the endpoint is `https://api.openai.com/v1`;
- `model:openai-compatible:<host>` for any other OpenAI-compatible Responses endpoint (a relay,
  proxy or self-hosted gateway); `<host>` is the lowercased hostname without the port.

The runtime MUST report its egress in `health.model_egress` and `capabilities.act.model_egress`,
and the endpoint host in `model_endpoint`. It MUST refuse a base URL that is not `https`
(plain `http` only on loopback) or that carries user information, a query or a fragment.

When an act request carries `model_egress` and it differs from the runtime's, the runtime MUST
answer `409 model_egress_mismatch` (with `details.runtime_model_egress`) **before** writing a
journal record or calling the model. The check comes after deduplication: a key already in the
journal is answered from the journal as in §6.2, so a client is never told "nothing happened"
about an act that did write. A request without `model_egress` (v0.3 clients) is not checked.
`model_egress` is not part of the request digest. The kernel maps the 409 to "no effect".

How the runtime talks to the model is not part of this protocol, but two runtime choices are
reported in `capabilities.act` because they explain what an endpoint must support:

- `variant`: `ga` (hosted `computer` tool), `preview` (`computer_use_preview`), or `function`
  (the same actions offered as a plain function tool, for endpoints that reject the hosted tool
  type). The action space (`wakecore.computer/1`) and every guard in §6.3 are the same for all
  three; the variant only changes the wire format to the model.
- `history`: `server` (turns chained with `previous_response_id`), `client` (the runtime resends
  the conversation, keeping only the most recent screenshots), or `auto` (server until the
  endpoint refuses chaining, then client). A refused turn reached no model and is retried once;
  replying to the model never repeats a page action.

## 7. Sessions

- `GET /v1/sessions` → `{sessions: [...]}`; `GET /v1/sessions/{ref}` → one session:
  `session_ref`, `state` (`open` | `closed` | `busy`), `profile_exists`, `last_used_at`.
  A runtime MUST NOT return cookies, storage contents or filesystem paths.
- `POST /v1/sessions/{ref}/release` → `{session_ref, released}` closes the browser holding the
  profile so that a human can open it to log in. It MUST NOT delete the profile.
- `POST /v1/sessions/release` with `{session_ref}` is the v0.3 form; deprecated, kept in v1.

## 8. Health and capabilities

- `GET /v1/health` → `ok`, `protocol`, `runtime_version`, `model_configured`, `model_ref`,
  `model_endpoint`, `model_egress` (§6.5),
  `sessions` (active refs), `interrupted_on_start` (how many journal records were turned
  `interrupted` at start-up).
- `GET /v1/capabilities` → `protocol`, `runtime_version`, `endpoints`, `extractor {modes, types,
  transforms, column_options}`, `act {available, action_space, variant, history, model_ref, model_endpoint,
  model_egress, cancel, max_steps_limit, guards}`, `limits {max_body_bytes, timeouts_max_s}`.

## 9. Security requirements (summary)

A conforming runtime MUST:

1. bind to loopback only and support a bearer token;
2. keep its state directory (profiles, journal, artifacts) private to its user (`0700`);
3. never accept or return credentials, cookies or storage contents over this protocol;
4. enforce `origins` on every request, redirect and popup, for observe, verify and act alike;
5. journal mutating requests before sending them, and never operate a key twice when a
   mutation may have been sent;
6. keep the model away from credential fields and never acknowledge safety checks on the
   user's behalf;
7. read the model API key from its own configuration (for example a `0600` key file), never
   from a request;
8. send screenshots only to the model endpoint it reports as `model_egress`, and refuse an act
   approved for another one (§6.5).

## 10. Conformance

`tests/contract/test_ui_runtime_protocol.py` and `tests/ui/test_protocol_live.py` exercise a
running runtime: every response is validated against the schemas, plus the 409 rule,
cancellation, session queries, the error envelope and protocol mismatch. An alternative
implementation (for example in TypeScript) can be pointed at by the same tests.
`tests/contract/test_openapi_drift.py` keeps the OpenAPI document equal to the route table.

## Appendix: endpoint index

| Method | Path | Request schema | Response schema |
|---|---|---|---|
| GET | `/v1/health` | — | `health.v1` |
| GET | `/v1/capabilities` | — | `capabilities.v1` |
| POST | `/v1/extractors/validate` | `extractor.v1#/$defs/validate_request` | `extractor.v1#/$defs/validate_response` |
| POST | `/v1/observe` | `observe.v1#/$defs/request` | `observe.v1#/$defs/response` |
| POST | `/v1/verify` | `verify.v1#/$defs/request` | `verify.v1#/$defs/response` |
| POST | `/v1/act` | `act.v1#/$defs/request` | `act.v1#/$defs/response` |
| GET | `/v1/acts/{key}` | — | `act_record.v1` |
| POST | `/v1/acts/{key}/cancel` | — | `act.v1#/$defs/cancel_response` |
| GET | `/v1/act/status?key=` | — | `act.v1#/$defs/status_response` |
| GET | `/v1/sessions` | — | `session.v1#/$defs/list_response` |
| GET | `/v1/sessions/{ref}` | — | `session.v1#/$defs/session` |
| POST | `/v1/sessions/{ref}/release` | — | `session.v1#/$defs/release_response` |
| POST | `/v1/sessions/release` (deprecated) | `session.v1#/$defs/release_request` | `session.v1#/$defs/release_response` |
