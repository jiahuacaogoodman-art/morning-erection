# Roadmap

Not a promise; a list of what we think is next, roughly in order. Each item keeps the ten
rules in [CONTRIBUTING.md](../CONTRIBUTING.md#rules-that-are-not-negotiable). Items that touch
the protocol will be additive within `wakecore.ui-runtime/1`.

> 中文：以下是计划而非承诺，大致按顺序排列；每一项都不能破坏约束 1–10，涉及协议的改动在 v1 内只做增量。

## Near term

- **A production planner adapter** for the reasoning port (a real model behind
  `plan`/`classify`, with its own egress target), so `"on_met": "plan"` works outside tests.
  The kernel already admits or rejects whatever a planner proposes; today the only
  implementation is scripted.
- **Inbox read API** (`GET /v1/inbox`, mark-as-read) so notifications can be read without
  opening the database.
- **Run the real Computer Use scenarios on more endpoints.** R2 and R3 passed on 2026-09-30,
  but only through one relay with `--variant function`. The hosted `computer` tool (`ga`)
  path has so far been verified only against the simulator.
- **Element-reference actions.** Let the model act on numbered elements from the page's
  accessibility tree instead of pixel coordinates (as browser-use and
  codex-computer-use-mcp do), with input values redacted before they leave the machine.
  Same guards, same journal.
- **Deterministic act descriptors.** Record the resolved action sequence of a successful act
  (selectors, not coordinates) so that a repeat of the same kind of action can run without a
  model, falling back to Computer Use when the page no longer matches. Still approved,
  journalled and verified like any act.
- **Origin denylist and private-network blocking.** Refuse loopback, link-local and RFC 1918
  addresses and a configurable denylist, on top of the per-task allow-list, including after
  DNS resolution.
- **Per-origin secret placeholders.** Let a task fill a form field from a secret reference that
  the sidecar resolves only for a named origin, so a model can ask for "the account number"
  without ever seeing it. Passwords and one-time codes stay out of scope; logins remain manual.

## Medium term

- **MCP server facade.** Expose granted tools and read-only task state over MCP. Approvals will
  not be grantable from an MCP client; see the next item.
- **Approval callback tokens.** Single-use, digest-bound links or tokens for approving an
  action from outside the management API (mail, chat), with expiry and audit.
- **Nested extractor records** (lists inside a card) once the kernel's condition language can
  address nested fields without losing determinism.
- **TypeScript sidecar** implementing the same protocol, checked by the same protocol tests.
- **Artifact access.** An authenticated, redacting endpoint for act screenshots and traces.

## Later

- Multi-node PostgreSQL deployment guide with failover testing; capacity measurements.
- Evaluations of model judgement (recall, false positives, prompt-injection resistance) with a
  published methodology; until then we make no quality claims.
- More connectors (mail, calendars) as separately installable plugins.
