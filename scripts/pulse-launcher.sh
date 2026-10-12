#!/bin/bash
# Morning Pulse launcher — a stable entry point so macOS only needs to authorize
# this script once in Accessibility, rather than re-authorizing every time the
# underlying Python binary changes (uv update, new cpython version, etc.).
exec "$(dirname "$0")/../.venv/bin/python" "$(dirname "$0")/../scripts/pulse.py" "$@"
