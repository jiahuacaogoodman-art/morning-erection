"""RFC §16: the kernel depends on ports only. No adapter, app, driver or network import."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "packages" / "wakecore" / "src" / "wakecore"
KERNEL = ROOT / "kernel"
FORBIDDEN = ("wakecore.adapters", "wakecore.app", "sqlite3", "psycopg", "psycopg2", "socket", "urllib",
             "http", "requests", "subprocess", "playwright", "openai")


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = _module_name(path).split(".")
    if path.name != "__init__.py":
        package = package[:-1]
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                out.append(".".join(base + ([node.module] if node.module else [])))
            else:
                out.append(node.module or "")
    return out


KERNEL_FILES = sorted(KERNEL.rglob("*.py"))


def test_kernel_files_found():
    assert len(KERNEL_FILES) > 20


@pytest.mark.parametrize("path", KERNEL_FILES, ids=lambda p: str(p.relative_to(KERNEL)))
def test_kernel_imports_no_adapters_or_drivers(path):
    bad = [m for m in _imports(path) if any(m == f or m.startswith(f + ".") for f in FORBIDDEN)]
    assert not bad, f"{path.relative_to(ROOT)} imports {bad}"


def test_domain_is_pure():
    """domain/ may only import the standard library and other domain modules."""
    for path in sorted((KERNEL / "domain").rglob("*.py")):
        for m in _imports(path):
            if m.startswith("wakecore."):
                assert m.startswith("wakecore.kernel.domain"), f"{path.name} imports {m}"


def test_relative_import_resolution_is_checked():
    # Sanity check for the resolver: executor imports from sibling kernel packages.
    mods = _imports(KERNEL / "execution" / "executor.py")
    assert any(m.startswith("wakecore.kernel.") for m in mods)


BROWSER_DRIVERS = ("playwright", "openai", "wakecore_ui_runtime", "ui_runtime", "simulators")


@pytest.mark.parametrize("path", sorted(ROOT.rglob("*.py")), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_wakecore_module_imports_a_browser_or_model_sdk(path):
    """V0.3 constraint 2: browser and Computer Use live behind the UI Runtime's HTTP API."""
    bad = [m for m in _imports(path) if any(m == f or m.startswith(f + ".") for f in BROWSER_DRIVERS)]
    assert not bad, f"{path.relative_to(ROOT)} imports {bad}"


def test_ports_depend_only_on_the_domain():
    for path in sorted((KERNEL / "ports").rglob("*.py")):
        for m in _imports(path):
            if m.startswith("wakecore."):
                assert m.startswith(("wakecore.kernel.domain", "wakecore.kernel.ports")), f"{path.name} imports {m}"


PUBLIC_SURFACES = {"protocol": ("wakecore.protocol",), "testing": ("wakecore.kernel", "wakecore.testing"),
                   "plugins.py": ("wakecore.kernel.ports",)}


@pytest.mark.parametrize("name", sorted(PUBLIC_SURFACES))
def test_public_surfaces_stay_small(name):
    """Contracts, the conformance suite and the plugin loader import no adapter, app or driver."""
    target = ROOT / name
    files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
    allowed = PUBLIC_SURFACES[name]
    for path in files:
        for m in _imports(path):
            if m == "wakecore" or m.startswith("wakecore."):
                assert m == "wakecore" or m in allowed or m.startswith(tuple(a + "." for a in allowed)), \
                    f"{path.relative_to(ROOT)} imports {m}"
            assert not any(m == f or m.startswith(f + ".") for f in FORBIDDEN if not f.startswith("wakecore")), \
                f"{path.relative_to(ROOT)} imports {m}"
