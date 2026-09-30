# Example: watch a web page with the eye, over the real API

Watch the public [TodoMVC demo](https://demo.playwright.dev/todomvc/#/) for a todo called
**"Pay rent"**. When it appears, WakeCore writes one notification to the internal inbox. When
it is ticked, the task completes. No model is involved: every round is a deterministic
Playwright read of the page.

The files here are the request bodies you send to the management API:

| File | Sent to | What it says |
|---|---|---|
| [`grant.json`](grant.json) | `POST /v1/grants` | The user allows `web.observe` + `inbox.notify_self`, only on `https://demo.playwright.dev`, with no model egress |
| [`binding.json`](binding.json) | `POST /v1/bindings` | The page is a `web.playwright` source. `secret_ref` names the browser profile, not a password |
| [`task_spec.json`](task_spec.json) | `POST /v1/tasks` | What to read (`resource_scope.extractor`, `record_ids`), when to wake (`decision`), when to stop (`complete_when`) |
| [`extractor.json`](extractor.json) | (inside the spec) | The extractor on its own, for `wakecore ui check-extractor`. See [docs/extractor.md](../../docs/extractor.md) |

TodoMVC keeps its list in the browser's `localStorage`, so the list lives in the sidecar's
browser profile. That is why the spec says `"state_location": "client"`.

## Run it

You need the development environment from the repository root:
`uv sync --all-packages --all-extras --group dev && uv run playwright install chromium`.

**1. Secrets and tokens.** The management API refuses to start without API tokens. The file
maps a bearer token to a principal and holds the binding's secret. Here the secret is only
the name of the browser profile (`todo_user`).

```bash
mkdir -p ~/.wakecore/ui-state && chmod 700 ~/.wakecore ~/.wakecore/ui-state
export API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
export WAKECORE_UI_RUNTIME_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
cat > ~/.wakecore/secrets.json <<EOF
{"secrets": {"local_user": {"sec_todo_session": "todo_user"}},
 "tokens":  {"$API_TOKEN": {"tenant_id": "local_user", "subject": "user:me"}}}
EOF
chmod 600 ~/.wakecore/secrets.json
export WAKECORE_SECRETS_FILE=~/.wakecore/secrets.json
export WAKECORE_DB=sqlite:///$HOME/.wakecore/dev.db       # PostgreSQL in production
export WAKECORE_UI_RUNTIME_URL=http://127.0.0.1:8765
```

**2. Start the sidecar and the kernel** (two terminals, same environment variables):

```bash
uv run wakecore-ui-runtime --state ~/.wakecore/ui-state --port 8765 --token "$WAKECORE_UI_RUNTIME_TOKEN"
```

```bash
uv run wakecore init-db
uv run wakecore serve-api --port 8080 --with-worker     # or serve-api + a separate `wakecore worker`
```

**3. Register, create, activate** (run from this directory):

```bash
H="Authorization: Bearer $API_TOKEN"; J="Content-Type: application/json"; API=http://127.0.0.1:8080
curl -s -H "$H" -H "$J" -H 'Idempotency-Key: grant-1'   -d @grant.json     $API/v1/grants
curl -s -H "$H" -H "$J" -H 'Idempotency-Key: binding-1' -d @binding.json   $API/v1/bindings
curl -s -H "$H" -H "$J" -H 'Idempotency-Key: task-1'    -d @task_spec.json $API/v1/tasks    # -> DRAFT + spec_digest
curl -s -H "$H" -H "$J" -H 'Idempotency-Key: act-1' -H 'If-Match: "1"' \
     -d '{"spec_version": 1, "spec_digest": "<spec_digest from the previous response>"}' \
     $API/v1/tasks/todo_watch/activate
```

A task is only a draft until you activate it with the exact digest you reviewed. Changing the
spec afterwards creates a new version, which has to be activated again.

**4. Be the user.** Open the same browser profile and add the todo by hand. With `--runtime`
the login helper first asks the sidecar to let go of the profile, because Chromium allows one
process per profile. The sidecar reopens the profile on its next read.

```bash
uv run wakecore-ui-login --runtime http://127.0.0.1:8765 --state ~/.wakecore/ui-state --session todo_user \
     --url 'https://demo.playwright.dev/todomvc/#/' --done-url-contains '#/completed'
```

The helper closes its window once the URL contains `--done-url-contains`. For this site that
means: add "Pay rent", then click the **Completed** filter. Wait for the next round
(`every_seconds: 60`), then run the helper again, tick the todo, and click **Completed**.
For a site with a real login you would pass the page you land on after signing in instead
(see [docs/runbooks/browser.md](../../docs/runbooks/browser.md)).

**5. Watch what happened.**

```bash
curl -s -H "$H" $API/v1/tasks/todo_watch            # lifecycle, explanation
curl -s -H "$H" $API/v1/tasks/todo_watch/timeline   # observations, decisions, actions
uv run wakecore replay --tenant local_user todo_watch   # read-only; never opens a page
```

## What a real run looked like

We ran exactly these steps on 2026-09-29 against the live demo site, with a script standing in
for the person at the keyboard. This is the timeline, audit entries omitted:

```
13:52:48 observation  success, complete
13:52:50 decision     NOOP  baseline_established            # "Pay rent" does not exist yet
13:53:48 observation  success, complete                     # (the user added "Pay rent")
13:53:51 decision     TEMPLATE_ACTION  watch_condition_met
13:53:51 action       inbox.notify  notify.condition_met  CONFIRMED
13:54:48 observation  success, complete
13:54:49 decision     NOOP  no_change                       # no second notification
13:55:48 observation  success, complete                     # (the user ticked it)
13:55:50 decision     NOOP  informational_only
lifecycle COMPLETED
```

`wakecore replay` reported `4/4` decisions matching and `0` external writes. The inbox message
it wrote was `New todo — Pay rent: todo_added done=False`.

## Limits of this example, stated plainly

- **Reading the inbox.** There is no management API endpoint that lists inbox messages yet.
  The message is in the `inbox_messages` table; the timeline shows the confirmed
  `inbox.notify` action. This is on the [roadmap](../../docs/roadmap.md).
- **Letting Computer Use tick the todo** (`"on_met": "plan"`, `browser.operate`, model egress
  `model:openai-computer-use`). The kernel, grant, approval and sidecar parts exist and are
  tested. But WakeCore does not yet ship a production planner model: the built-in reasoning
  adapter is scripted. With `"on_met": "plan"` over this API, the planner proposes nothing, and
  no browser action is created. `tests/real` R2 covers this path with a scripted plan and a
  real model. It passed on 2026-09-30 through a relay with `--variant function`. With an
  OpenAI-compatible relay the egress is `model:openai-compatible:<host>` instead; see the
  [browser runbook](../../docs/runbooks/browser.md) (§1.1).
- The demo site is public and shared. Use your own site and origin for anything real, and keep
  the `--state` directory private: it holds browser profiles, which means logged-in sessions.
