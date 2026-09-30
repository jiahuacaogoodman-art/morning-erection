"""WakeCore UI Runtime (V0.3 P2): the browser "eyes and hands", as a separate process.

Playwright = eyes (deterministic DOM/A11y extraction, no LLM), OpenAI Computer Use = hands
(only for interactive operations), and this sidecar owns everything a model or the
kernel must never hold: browser profiles, cookies, the login itself, screenshots, traces.
WakeCore talks to it over local HTTP and only ever passes an opaque `session_ref`.

The wire protocol (docs/spec/ui-runtime-protocol.md) is language-neutral; this is the
reference implementation in Python. The module layout (sessions/manager,
browser/{playwright,observer,extractor}, cua/openai, verify/verifier, server) maps
one-to-one onto a TypeScript port.
"""
from ._version import __version__

__all__ = ["__version__"]
