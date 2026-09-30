# Contributing to WakeCore

[中文版](CONTRIBUTING.zh-CN.md)

Thanks for helping. WakeCore is a runtime whose whole value is in guarantees (an effect
happens at most once, an approval covers exactly one payload, replay never touches the
world), so changes are reviewed first for what they could break, then for what they add.

## Setting up

You need [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
git clone https://github.com/jiahuacaogoodman-art/morning-erection && cd morning-erection
uv sync --all-packages --all-extras --group dev     # one .venv for the whole workspace
uv run playwright install chromium                  # only for tests/ui and the sidecar
uv run wakecore demo                                # offline demo: fake clock, no model, no sends
```

Layout:

| Path | What |
|---|---|
| `packages/wakecore/src/wakecore` | The kernel (`morning-erection` on PyPI). **Standard library only.** |
| `packages/wakecore-ui-runtime/src/wakecore_ui_runtime` | The browser sidecar (`morning-erection-ui-runtime`). Playwright, the Computer Use loop. |
| `packages/wakecore/src/wakecore/protocol` | Shared contract: JSON Schemas, the schema validator, the OpenAPI generator. |
| `spec/` | Generated OpenAPI documents. Never edit by hand. |
| `tests/` | All tests (not shipped in wheels). |
| `examples/` | A plugin package and task examples. |
| `docs/` | Protocol spec, extractor reference, ADRs, runbooks. |

## Tests

| Command | What it covers | Needs |
|---|---|---|
| `uv run pytest -q --ignore=tests/ui` | Unit, contract, security, recovery (incl. randomised fault injection), replay | nothing |
| `WAKECORE_PG_DSN=postgresql://… uv run pytest -q tests/integration_postgres` | PostgreSQL concurrency gate (leases, slots, send-once under contention) | a disposable database |
| `WAKECORE_TEST_BACKEND=postgres WAKECORE_PG_DSN=… uv run pytest -q --ignore=tests/ui` | Every harness test on PostgreSQL | same |
| `uv run pytest -q tests/ui` | Real Chromium + sidecar process + simulated portal and simulated OpenAI | Playwright + Chromium |
| `WAKECORE_REAL=1 uv run pytest -q -s tests/real` | Public test websites and (with a key) the real Computer Use model | network, optional key |
| `WAKECORE_REAL=1 WAKECORE_REAL_DESKTOP=1 uv run pytest -q -s tests/real_desktop` | The real macOS Calculator through the real MCP bridge, and (with a key) a real model | macOS, Node, the bridge, Accessibility permission |
| `uv run ruff check` | Lint | — |
| `uv run python scripts/gen_openapi.py --check` | The committed OpenAPI documents match the route tables | — |

A local disposable PostgreSQL: see [docs/runbooks/postgres.md](docs/runbooks/postgres.md).
`tests/real` and `tests/real_desktop` never run in CI; the key for it lives in `.secrets/openai.key` (git-ignored,
`chmod 600`) and only the sidecar process reads it.

Before you open a pull request, run at least the first, the lint and the OpenAPI check.
If you touched the sidecar, run `tests/ui`. If you touched storage, leases or the executor,
run the PostgreSQL gate.

## Rules that are not negotiable

These are enforced by tests where possible (`tests/unit/test_architecture.py`,
`tests/security`, `tests/recovery`). A pull request that weakens one will not be merged,
however useful the feature.

1. Do not rewrite `ObservationPort`, `ActionPort`, leases, the scheduler, approvals, the
   outbox or the `UNKNOWN`/reconcile state machine. Extend them; propose changes in an ADR first.
2. Browsers, models and SDKs are adapters or sidecars. The kernel never imports Playwright,
   an OpenAI/Anthropic SDK, Chrome tooling or database drivers outside `adapters/`.
3. High-frequency watching uses deterministic extraction (no model call).
4. Computer Use is only for complex, unfamiliar or interactive operations.
5. Passwords, cookies and 2FA tokens never enter a model context and are never stored in
   WakeCore's business tables.
6. The planner only sees the tools the current task is actually granted.
7. Every browser action is bound to its allowed origins / `resource_scope`.
8. `data_egress`, payload and resource scope are bound together into the action digest and
   the approval.
9. Every submit/delete/upload is verified after it runs.
10. Replay only recomputes from records. It never operates a web page or causes a side effect.

Also:

- The kernel's layering is `domain` ← `ports` ← `kernel` ← `adapters` ← `app`; `protocol`,
  `testing` and `plugins.py` stay small public surfaces. The architecture test checks this.
- A new tool or connector must pass `wakecore.testing.conformance` (see
  [examples/custom_connector](examples/custom_connector)).
- Protocol changes are additive within a major version. Add fields as optional; never change
  the meaning of an existing field or status. Update the JSON Schema, regenerate `spec/`
  (`uv run python scripts/gen_openapi.py`) and the normative text in
  [docs/spec/ui-runtime-protocol.md](docs/spec/ui-runtime-protocol.md).
- Storage changes come with a numbered SQL migration and keep the catalogue test green.
- Honesty in docs: say what was verified and on what. "Not verified" is an acceptable answer.

## Commits and sign-off (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/) instead of a CLA.
Sign every commit:

```bash
git commit -s -m "sidecar: refuse act when origins is empty"
```

This adds `Signed-off-by: Your Name <you@example.com>`, stating you have the right to submit
the change under the project's MIT licence. Keep commits focused; describe *why* in the body.
Add a line to `CHANGELOG.md` under *Unreleased* for anything user-visible.

## Reporting bugs and security issues

Use the issue forms. **Never paste credentials, cookies, API keys, session files or
screenshots of logged-in pages.** Security issues go through private reporting, see
[SECURITY.md](SECURITY.md).

By participating you agree to the [code of conduct](CODE_OF_CONDUCT.md).
