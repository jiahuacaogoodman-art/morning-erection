# ADR 0010: Agent first, with Pi execution and Pulse as a capability

The default runtime decisions below are superseded by [ADR 0011](0011-native-pi-and-gateway-credentials.md).
The scoped adapter remains a regression reference, not the active Morning implementation.

Status: accepted for the local application.

The primary product is a general Agent. Website observation is one background capability.
Remove example watches, preset task chips, and mandatory extractor/operator forms from the
normal conversation. A real model interprets goals and relevant page content; ambiguity is
resolved with a short question. User confirmation activates a concrete, grounded watch.
Semantic conditions use a real classifier only when relevant trusted content changes;
unchanged polls remain model-free. Numerical conditions can be compiled to deterministic
checks after language understanding. A keyword parser is not used as language understanding.

## Reuse from Pi

Use the published MIT-licensed `@earendil-works/pi-agent-core`, `pi-ai` and `pi-coding-agent`
packages at 0.99.1 (official source `earendil-works/pi`, inspected revision
`d2931ad3d5bf6936fbdfa5dfc81fb32f875499b2`). Agent Core supplies the model/tool/result loop,
event streaming and conversation state. Coding tool factories supply read/write/edit/bash,
exact-match edit behavior, output truncation, partial shell output and file mutation queues.
This is actual SDK reuse, not a visual imitation or a renamed custom loop.

A Node sidecar behind a stdlib Python bridge keeps Node, SDKs and processes outside the
durable kernel. Provider credentials are sent over stdin, never command-line arguments.
Use namespaced model-facing tool names to avoid compatible-provider built-in-name collisions.
Tool labels and actual results are shown in the main conversation, with long details folded.
Messages, tool results and session context are saved locally. No content is auto-published.

## Workspace boundary

Pi itself does not provide a permission sandbox. Inject filesystem operations that resolve
and validate the selected workspace, including symlink targets; protect credential/runtime
paths. Read existing files before overwriting, preserve local backups, and use Pi's precise
edit and per-file mutation queue. On macOS, bash runs through sandbox-exec with workspace
writes, filtered environment and no network access. Other systems require an isolated bash
executor; never silently fall back to unrestricted shell execution.

The durable scheduler, model budgets, browser approval digests, send-once journal and
post-action observation remain WakeCore responsibilities. General coding tools are not
allowed to weaken those boundaries or turn a proposed browser action into a direct write.

## Verification boundaries

Test tool mutations, ambiguity rejection, workspace escapes, credential symlinks, isolated
commands, cancellation/timeouts and conversation progress with actual results. Verify at
least one real model read -> edit -> write -> run -> result loop. Keep stub-model tests
distinct from real-model checks. Unavailable or interrupted models never return an assumed
completion. Published Pi license/notices stay in dependencies; local provenance is in
`apps/agent-runtime/THIRD_PARTY.md`.
