# How WakeCore compares, and what it borrowed

WakeCore was designed after reading how neighbouring open-source projects solve parts of the same
problem. This page says, for each of them, what we took and where we deliberately differ.
Descriptions of other projects reflect their public documentation and source as of
September 2026 and are necessarily simplified; corrections are welcome.

**In one sentence:** durable-execution engines make *code* survive crashes, agent frameworks
make *models* call tools, browser agents make *models* operate pages; WakeCore is the narrow
layer that decides whether a model's proposed side effect may happen, makes it happen at most
once, proves it happened, and watches the world cheaply in between.

## Browser agents and browser infrastructure

| Project | What it is | What we borrowed | Where we differ |
|---|---|---|---|
| [browser-use](https://github.com/browser-use/browser-use) | Python agent that drives a browser with an LLM | Allowed-domain restrictions and the idea of keeping credentials out of the prompt (its sensitive-data placeholders inspired our roadmap item on per-origin secret placeholders) | The model is not in the watch loop at all; every page write is an approved, digest-bound, journalled act, and success is decided by a deterministic re-read, not by the agent |
| [Stagehand](https://github.com/browserbase/stagehand) | TypeScript SDK with `act` / `extract` / `observe` primitives | The split between reading and acting, and caching resolved actions so repeats need no model (roadmap: deterministic act descriptors) | Our `extract` is declarative CSS with a strict schema (no model), so a drifted page is `SCHEMA_INVALID` rather than a plausible guess |
| [Skyvern](https://github.com/Skyvern-AI/skyvern) | Vision-LLM workflows for browser tasks | Treating verification as its own step after an action | Verification is mandatory and deterministic; an unverified write stays `UNKNOWN` and is reconciled by observation, never retried blindly |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | MCP server exposing Playwright to models | Origin allow-listing at the browser layer; accessibility-first reading | We enforce origins on every request, redirect and popup, and bind them into the approval digest; the kernel, not the model, chooses the origins |
| [Steel](https://github.com/steel-dev/steel-browser) | Browser sessions as an API | Sessions as first-class, queryable resources (`GET /v1/sessions`, release) | Profiles never leave the sidecar: no cookie or storage export endpoint exists |
| [crawl4ai](https://github.com/unclecode/crawl4ai) | Crawler with schema-based CSS/XPath extraction | Declarative JSON extraction schemas; per-field regex, types and defaults (extractor v1) | Fewer features on purpose (no nesting, no crawling) and strict failure: a missing field is an error unless a `default` is declared |
| [OpenAI CUA sample app](https://github.com/openai/openai-cua-sample-app) | Reference computer-use loop | The computer-use action space and the pending-safety-check flow | We never acknowledge a safety check on the user's behalf; it ends the act as `needs_human` |
| [Anthropic computer-use demo](https://github.com/anthropics/anthropic-quickstarts) | Reference computer-use agent in a container | Running the operator in a separate, isolated process | Our sidecar is protocol-first (JSON Schema, OpenAPI), so it can be reimplemented in another language or sandbox |

## Durable execution

| Project | What it is | What we borrowed | Where we differ |
|---|---|---|---|
| [Temporal](https://github.com/temporalio/temporal) | Durable workflows from an event history | Deterministic replay from recorded history; activities as the only place side effects happen | Replay never calls an adapter, not even a read. After a crash, an activity-like action whose effect is uncertain becomes `UNKNOWN` and is resolved by observing the world, not by retry policy |
| [Restate](https://github.com/restatedev/restate) | Durable handlers with a journal | A journal written ahead of the side effect | The journal also lives in the sidecar, at the level of individual HTTP writes a browser makes |
| [DBOS](https://github.com/dbos-inc/dbos-transact-py) | Durable workflows stored in Postgres | Postgres as the single source of truth; idempotency derived from a stable ID | We add leases fenced by an epoch, execution slots and budgets as first-class tables |
| [Inngest](https://github.com/inngest/inngest) | Event-driven step functions | Event ingress with deduplication; waiting for events as a step | Ingress is HMAC-signed and only ever persists events: nothing inside an event can activate, approve, cancel or grant anything |

WakeCore is not a general workflow engine: it runs one shape of task (observe → decide → act →
verify) and gives that shape stronger guarantees than a general engine can without knowing
what the side effect is.

## Agent frameworks and protocols

| Project | What it is | What we borrowed | Where we differ |
|---|---|---|---|
| [LangGraph](https://github.com/langchain-ai/langgraph) | Stateful agent graphs with checkpointers and `interrupt()` | The checkpointer conformance-test idea → `wakecore.testing.conformance` for connectors and tools | Human approval is bound to a digest of payload, egress and resource scope; changing any of them invalidates it |
| [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) | Agents, tools, guardrails, handoffs | Guardrails as a separate, non-model check; tool approval before execution | The planner only ever sees tools the task is granted; policy is computed (`EffectiveAuthority`), not prompted |
| [Model Context Protocol](https://modelcontextprotocol.io/specification) | Tool/resource protocol for models | Tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) → `to_mcp_tool` | MCP treats annotations as untrusted hints. Ours are derived from the tool descriptor the kernel enforces, and the enforced guarantees travel in `_meta` |
| [Stripe idempotency](https://docs.stripe.com/api/idempotent_requests), [IETF Idempotency-Key draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/) | Idempotent HTTP writes | Reusing a key with a different request is an error, not a replay | The draft suggests 422 for a mismatched payload and 409 for a concurrent request. v1 answers 409 `idempotency_key_reused` for a mismatch and `200 in_progress` for a concurrent duplicate; this is fixed for v1 |

## Project structure

We also copied structure, not only ideas: a uv workspace with `src/` layout and separate
distributions (as in [pydantic-ai](https://github.com/pydantic/pydantic-ai)); plugins found
through entry points and loaded only when allowlisted (as Airflow providers and pytest plugins
are discovered); generated OpenAPI documents with a drift test; issue forms that ask for the
effect on guarantees; and DCO instead of a CLA.

## When not to use WakeCore

- You need arbitrary long-running business workflows: use a durable-execution engine.
- You want a model to explore a website freely and report back: use a browser agent.
- Your task has no external side effects: most of WakeCore's machinery will not pay for itself.
