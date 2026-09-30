# Security policy

WakeCore runs long-lived tasks that hold access to other systems: browser profiles with live
logins, tool credentials, approval decisions. Please treat security reports accordingly.

## Reporting a vulnerability

**Do not open a public issue.** Use GitHub's private vulnerability reporting
(repository → *Security* → *Report a vulnerability*). Include the version, the component
(kernel, UI Runtime sidecar, a specific adapter), a minimal reproduction and the impact you
expect. We aim to acknowledge within 5 working days and to agree a disclosure date with you;
the default embargo is 90 days or until a fixed release is out, whichever is sooner.

Never include real credentials, cookies, session files, API keys or screenshots of logged-in
pages in a report, an issue, a discussion or a pull request. Redact them, or describe them.

## Supported versions

WakeCore is pre-1.0. Only the latest minor release receives fixes.

| Version | Supported |
|---|---|
| 0.3.x | yes |
| < 0.3 | no |

## Threat model (what WakeCore defends, and what it assumes)

What the design guarantees, and where each guarantee lives:

| Guarantee | Enforced by |
|---|---|
| Credentials (passwords, cookies, 2FA seeds, tokens) never enter a model context and are not stored in WakeCore's business tables | Secrets are referenced by `secret_ref`; the kernel resolves them only inside the adapter call. The sidecar keeps browser profiles in its own state directory. |
| The planner only sees tools the task was granted | `EffectiveAuthority` (grant ∩ system policy ∩ task spec) filters the tool list before any model call. |
| A browser action can only reach the origins it was authorised for | `resource_scope.origins` is bound into the action digest; the sidecar enforces an origin allow-list on every request, popup and redirect. |
| An approval covers exactly one payload, egress set and scope | The approval stores the action's `payload_digest`; any change (`revise`) needs a new approval. |
| An external effect happens at most once per `effect_key` | Write-ahead journal + send-once in the tool/sidecar; an unclear outcome becomes `UNKNOWN` and is reconciled by re-observation, never by re-sending. |
| Every side effect is verified after it runs | Tools return `confirmed` only after a deterministic re-observation (or provider receipt). |
| Replay never touches the outside world | `wakecore replay` recomputes from recorded evidence only; no adapter is called. |

What WakeCore assumes, and what you must do as an operator:

- **The sidecar state directory is a credential store.** It contains browser profiles with
  live sessions. Keep it `0700`, on an encrypted disk, owned by the service user, out of
  backups you would not trust with passwords.
- **The sidecar listens on 127.0.0.1 only and must not be exposed.** Start it with `--token`
  (or `WAKECORE_UI_RUNTIME_TOKEN`) whenever anything else runs on the machine.
- **The management API is not a public API.** Put it behind your own authentication and TLS
  if it leaves localhost. Ingress endpoints require an HMAC signature; rotate ingress secrets.
- **API key files must be `0600`.** Pass the model key with `--openai-key-file`; do not put it
  in the task spec, the command line of other processes, or the environment of the kernel.
- **A model endpoint sees every screenshot of an act.** That includes whatever the page shows
  while logged in. An OpenAI-compatible relay (`--openai-base-url`) is therefore a separate
  data recipient: its egress is `model:openai-compatible:<host>`, the grant and approval must
  name it, and the sidecar refuses an act approved for a different endpoint before calling any
  model. Only use a relay you would trust with those screenshots and with your key.
- **Plugins are arbitrary code.** A plugin receives resolved credentials and can perform side
  effects. Only entry points named in `WAKECORE_PLUGINS` are loaded; review a plugin before you
  allowlist it, and pin its version.
- **Model output is a proposal.** Prompt injection from a watched page can influence what the
  model proposes; it cannot widen the grant, change the origins, or skip approval. Keep
  approval on for `external_write` tools (the default).
- **Websites can perform effects on GET.** Such effects are not in the sidecar's write-ahead
  journal and can only be detected by re-observation. Scope origins tightly.

Out of scope: attacks that require control of the host, the database, or the sidecar state
directory; denial of service against a single-node deployment; model quality (a wrong but
authorised proposal that a human approved).
