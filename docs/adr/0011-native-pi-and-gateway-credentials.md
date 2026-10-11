# ADR 0011: Native Pi sessions and managed Gateway credentials

Status: implemented and verified on macOS with the existing API-key Gateway, 2026-10-11.

Morning must expose the existing Pi agent before adding application-specific automation.
The current installation pins Pi 0.99.1. Its shipped SDK documentation and TypeScript
declarations are the implementation reference; do not guess APIs from older integrations.

## Findings

- Conversation routing currently splits ordinary replies and coding into different contexts.
- The bridge awaits a full prompt in the input-reading loop, preventing live interruption.
- Session history is assigned to `session.state.messages` while SessionManager is in-memory.
  Pi explicitly documents SessionManager as authoritative; this assignment does not restore
  persistent context or its entry tree.
- Skills, context discovery, templates, themes, and extensions are explicitly disabled.
- Tool selection and a 24-turn cap override native behavior. Model capabilities are hard-coded.
- Gateway configuration exists only at launch time. GUI credential lifecycle is missing.

## Decisions

1. Gateway lifecycle is P0: named profiles, address/protocol/model, add/update/remove credentials,
   safe persistence, real model connection tests, classified errors, and restart recovery.
2. Store secret credential objects in the operating-system credential vault. Public profile
   metadata may live in a private JSON file. Never return credentials in settings/state, include
   them in argv, or log protocol frames containing secrets. No silent plaintext fallback.
3. Use Pi's `CredentialStore`/`ModelRuntime` contracts; the vault adapter changes storage,
   not provider authentication. Preserve native credential refresh serialization.
4. Use `AgentSessionRuntime`, persistent `SessionManager`, `DefaultResourceLoader`, native
   tools and native model metadata. Remove the general-chat classifier as an entry gate.
5. Run Pi's exported `runRpcMode(AgentSessionRuntime)` unchanged for the public agent protocol.
   Do not write another prompt dispatcher. GUI consumes its native commands, events and
   extension UI subprotocol, including steer/follow-up, abort, sessions, model/thinking,
   context/compaction and slash commands. A separate inherited private socket carries
   credential persistence and application-specific control, keeping secrets out of the
   public RPC stream and command-line arguments.
6. Add the Pi CLI's codemode/tool_search/MCP extensions through its official SDK factories.
   Expose resource diagnostics instead of swallowing load failures.
7. Browser inspection and WakeCore watch creation/control are additive tools. Keep WakeCore's
   durable scheduling, authority, exact approvals, and post-action verification unchanged.
8. Preserve the new Morning visual identity. Add restrained sunrise/loading/feedback motion
   after Gateway and native Pi execution acceptance passes, respecting reduced motion.

## Acceptance

- Real existing API-key Gateway: GUI configuration -> authentication/model test -> Pi ordinary
  conversation -> actual read/edit/write/command result. Repeat after process restart.
- Credential create/update/delete and invalid-key feedback; response/log/state redaction.
- Requirements discussed before coding reach the same native session; separate sessions
  remain separate; restore and branch use native persisted entries.
- Interrupt during a running command; steer/follow-up during a real run; no false completion.
- Skills, project instructions, templates, extension tools/commands/dialogs and resource reload
  are exercised through GUI controls. Compaction and native settings are not overridden.
- Distinguish real Gateway checks from deterministic regression fixtures. Native terminal-only
  extension rendering must be documented if it cannot be represented in the web GUI.

Source basis: Pi package `docs/sdk.md`, `docs/rpc-commands.md`, `docs/rpc-extension-ui.md`,
`dist/core/agent-session-runtime.d.ts`, `dist/core/model-runtime.d.ts`, and pi-ai
`dist/auth/types.d.ts`. Public project: https://pi.dev/ .

Implementation and verification details, including the native RPC limitations, are in
[the acceptance record](../verification/2026-10-11-native-pi-gateway.md).
