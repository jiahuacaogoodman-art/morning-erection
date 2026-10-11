# Morning Agent · Pulse

Morning is an Agent first. Conversation is the primary surface, actual coding tools and
results stay in the conversation, and Pulse represents background observation.

The default UI contains no example watches, preset task chips, selector picker or condition
form. A real model understands requests and page context, asks essential questions and
presents a short proposal before a watch begins. Typed conditions can become deterministic
checks; meaning-based conditions are judged only when relevant observations change.

```bash
# apps/agent-runtime
npm ci --ignore-scripts
npm run prepare:runtime
npm test
# apps/pulse
npm ci
npm run build
# repository root
uv sync --all-packages --all-extras --group dev
uv run python scripts/pulse.py
```

Open `http://127.0.0.1:8788`. The launcher uses existing local model configuration under
`.secrets` or the documented environment variables, starts a local browser runtime when
one is not supplied, and exposes the Pi coding runtime. `--workspace` selects the scope.

See [the Agent runbook](../../docs/runbooks/pulse.md) and
[Pi SDK provenance](../agent-runtime/THIRD_PARTY.md). `--demo` and simulated sites are
acceptance fixtures, not product presets. CI uses model doubles; real-model verification
is reported separately.
