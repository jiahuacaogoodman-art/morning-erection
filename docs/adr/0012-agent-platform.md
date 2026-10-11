# ADR 0012: Pi, Computer and source-neutral Watch

Status: local platform implemented and exercised on macOS, 2026-10-11.
Cross-machine and precise multi-window acceptance remain explicitly out of the verified scope.

## Audit

Pi 0.99.1 already runs its exported RPC loop, persistent SessionManager, native
tools, model configuration, resource discovery, MCP and extension UI. Keep the
pinned runtime and its public protocol. Model Gateway credentials already use
the macOS Keychain; device transport credentials must use a separate namespace.

WakeCore already has durable scheduling, versioned task specifications, grants,
change deduplication, approvals, recovery and browser/desktop connectors. Extend
these ports rather than replace the scheduler. The existing desktop bridge is
codex-computer-use-mcp 0.5.2; its model-free MCP methods can be called directly by
Pi. Keep the existing guarded desktop action adapter for legacy tasks.

Missing: console Computer/Watch/Tasks/Devices entries; direct computer steps and
takeover; general local/network sources; module lifecycle and shared events.

## Boundaries

- Pi owns conversation, tools, context and all model choices.
- A separately runnable Computer node owns observation and input, scoped device
  permissions, fresh observation references and durable send-once records.
- Watch owns source adapters and runs in the existing worker independently of
  the browser tab. Its CLI may run without Pi or the console.
- The console presents real state and human decisions. It cannot access OS
  permissions directly. Device tokens never share model-provider credentials.
- Computer writes require a concrete, expiring approval bound to the observed
  application and exact arguments. Unknown outcomes are observed, never retried.
- Module switches leave native Pi tools/resources untouched.
- Events use version, sequence, type, entity, time and payload; consumers resume
  with a sequence cursor. Sensitive action text and credentials stay out of logs.

## Product contract

Keep the layered sunrise mark, warm neutral surfaces, charcoal text and muted
terracotta accent. Agent stays the first route; Pulse stays compact. Six clear
navigation entries lead to dedicated workflows, with advanced configuration
collapsed. Empty states state actual availability and offer the next valid action.

Considered: (A) quiet workspace, reusing existing shell; (B) technical operations
grid, denser but distracts from chat; (C) spacious editorial pages, distinctive but
slower for device operation. Choose A for continuity, task efficiency and small
implementation scope. Use 1440x900 and 390x844, light/dark, with keyboard access,
visible focus, labelled controls and reduced motion.

Acceptance evidence and unresolved dependencies are recorded separately in
`docs/verification/2026-10-11-agent-platform.md` as work is verified.
