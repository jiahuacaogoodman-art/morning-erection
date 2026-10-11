# Pi SDK provenance

Morning reuses the following official packages from [Pi](https://pi.dev/) and
[earendil-works/pi](https://github.com/earendil-works/pi), each pinned to 0.99.1:

- `@earendil-works/pi-agent-core`: execution loop, message state and tool lifecycle events.
- `@earendil-works/pi-ai`: provider/model integration and Responses streaming.
- `@earendil-works/pi-coding-agent`: AgentSessionRuntime, persistent SessionManager,
  ModelRuntime, default resources/tools, package management, native RPC and extension UI.

Inspected upstream revision: `d2931ad3d5bf6936fbdfa5dfc81fb32f875499b2`.
The packages are MIT licensed, copyright Mario Zechner; their copyright and license notices
remain in the installed distributions. Morning's vault storage, private process channel,
GUI projection and WakeCore tools are local adapters. The default host delegates the agent
protocol to `runRpcMode` unchanged. Its two version-pinned deep imports reuse the CLI HTTP
dispatcher and project-trust resolver; recheck them whenever the dependency version changes.
The optional Responses instructions-field compatibility adapter wraps the official provider
stream function and preserves the caller's native payload/response hooks.

The upstream CLI shrinkwrap reinstalls `brace-expansion` 5.0.9 despite a root override.
Install with `npm ci --ignore-scripts`, then run `npm run prepare:runtime` to link the
explicitly pinned 5.0.12 package and synchronize the lockfile before `npm audit` and
`npm test`. The Python bridge also checks this preparation before starting the runtime.
Installation lifecycle scripts are not required for this adapter.
