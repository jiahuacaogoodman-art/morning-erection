# ADR 0007: Open-source layout, UI Runtime protocol v1 and public kernel interfaces

- Status: accepted (0.3.0)
- Related: ADR 0001 (stdlib kernel), ADR 0006 (browser eye and hand), [docs/comparison.md](../comparison.md)

> 中文摘要：拆成 uv workspace 下两个 `src/` 布局的发行包（内核只用标准库）；UI Runtime 协议以 JSON Schema 为单一来源，
> 定为 `wakecore.ui-runtime/1`，主版本内只做增量；增加统一错误信封、能力/版本协商、act 查询与协作式取消、会话查询、
> 同 key 不同请求 409；内核新增插件（entry point + 白名单）、一致性测试套件、MCP 注解映射、生成的 OpenAPI。
> 约束 1–10 与内核状态机均未改动。

## Context

Before 0.3.0 the kernel, the sidecar and the simulators lived in one flat tree; the sidecar's
HTTP interface was described only in a module docstring; errors had several shapes; there was
no way for a third party to add a connector or tool without editing the kernel; and nothing
told an alternative implementation what "correct" meant. Publishing the project needed a
layout others can depend on and a contract others can implement.

## Decision

1. **Two distributions, one workspace.** `packages/wakecore` (import `wakecore`, dependencies:
   none) and `packages/wakecore-ui-runtime` (import `wakecore_ui_runtime`, depends on
   Playwright and on the kernel of the same version). `src/` layout; SQL migrations, JSON
   Schemas and the demo spec are package data. Tests stay at the repository root and are not
   shipped. The distribution names (`morning-erection`, `morning-erection-ui-runtime`, and the
   GitHub repository `morning-erection`) appear only in the two `pyproject.toml` files, the
   READMEs and the links; the import names stay `wakecore` / `wakecore_ui_runtime`.
2. **The protocol is a document, not a module.** `wakecore/protocol/schemas/ui_runtime/*.v1.schema.json`
   is the single source of truth. The sidecar validates every request body against it with
   `wakecore.protocol.jsonschema_lite` (stdlib); responses are validated in tests; the OpenAPI
   document is generated from the route table and checked for drift; the normative text is
   [docs/spec/ui-runtime-protocol.md](../spec/ui-runtime-protocol.md).
3. **Versioning.** `wakecore.ui-runtime/1` in a response header and in `health`. Changes within
   a major are additive only. The client negotiates on first use and refuses a different
   major before sending anything, which the kernel maps to "no effect".
4. **New protocol surface, all additive:** one error envelope with stable codes and
   `retryable`; `capabilities`; offline extractor validation; the full act record; cooperative
   cancel; session queries; 409 for an act key reused with a different request (digest over
   session, goal, start URL, origins and login markers); optional per-request timeouts;
   extractor v1 conversions (regex, number, transform, declared default). Every v0.3 endpoint
   and field still works; two are marked deprecated.
5. **Cancel is not a new kernel state.** A cancelled act is reported as `canceled` with its
   mutation count and handled exactly like any other failed act: zero mutations → no effect,
   otherwise `UNKNOWN` → reconcile by observation.
6. **Kernel extension points.** Entry-point groups `wakecore.connectors` / `wakecore.tools`,
   loaded only when named in `WAKECORE_PLUGINS` (a plugin can reach credentials); a failing
   plugin is logged and skipped and cannot replace a built-in. `wakecore.testing.conformance`
   is the contract a plugin must pass. `to_mcp_tool` exports descriptors as MCP tool
   definitions (export only; WakeCore does not become an MCP server in 0.3).
7. **Project files**: MIT, DCO, security policy with a threat model, CI that proves the kernel
   installs and passes its tests with nothing but the standard library.

## Consequences

- An alternative sidecar (for example TypeScript, or one running in a remote sandbox) can be
  written from the spec and checked with the protocol tests.
- The kernel's state machine, ports, leases, scheduler, approvals, outbox and reconciliation
  are unchanged (constraint 1). The extension points add code paths *around* them.
- Two version numbers must be bumped together; the release workflow checks that the tag
  matches both.
- `jsonschema_lite` implements only the keywords our schemas use. A schema that needs more
  must extend it with tests, not pull in a dependency.
- The 409-vs-422 choice for key reuse differs from the IETF Idempotency-Key draft and is now
  fixed for v1.

## Alternatives considered

- **Keep one package, make Playwright an optional extra.** Rejected: the sidecar is a separate
  process with a different security posture; a separate distribution makes the stdlib-only
  kernel checkable in CI.
- **Pydantic / FastAPI for the sidecar.** Rejected for 0.3: it would add dependencies to the
  shared contract and generate the schema from code, when we want code checked against a schema.
- **gRPC.** Rejected: localhost JSON over HTTP is debuggable with curl and implementable anywhere.
- **Expose WakeCore as an MCP server now.** Deferred (roadmap): the approval flow needs a
  callback design first, or MCP clients would be tempted to auto-approve.
