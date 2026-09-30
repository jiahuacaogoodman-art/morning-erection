"""Regenerate the committed OpenAPI documents from the live route tables.

    uv run python scripts/gen_openapi.py          # write spec/*/openapi.v1.json
    uv run python scripts/gen_openapi.py --check  # exit 1 if they are stale (what CI's drift test does)
"""
import sys
from pathlib import Path

from wakecore.app.cli.main import kernel_openapi
from wakecore.protocol.openapi import dumps
from wakecore_ui_runtime.desktop.server import openapi_json as desktop_openapi_json
from wakecore_ui_runtime.server import openapi_json

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    ROOT / "spec" / "ui-runtime" / "openapi.v1.json": openapi_json,
    ROOT / "spec" / "desktop-runtime" / "openapi.v1.json": desktop_openapi_json,
    ROOT / "spec" / "kernel-api" / "openapi.v1.json": lambda: dumps(kernel_openapi()),
}


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for path, make in TARGETS.items():
        text = make()
        if path.exists() and path.read_text(encoding="utf-8") == text:
            continue
        stale.append(path.relative_to(ROOT))
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    for p in stale:
        print(("stale: " if check else "wrote: ") + str(p))
    return 1 if check and stale else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
