"""Third-party connectors and tools, discovered through Python entry points.

A plugin distribution declares factories in its pyproject:

    [project.entry-points."wakecore.connectors"]
    acme_crm = "acme_wakecore:make_connector"

    [project.entry-points."wakecore.tools"]
    acme_ticket = "acme_wakecore:make_tool"

A factory is `(PluginContext) -> adapter`; the adapter implements ObservationPort (connectors)
or ActionPort (tools). Installing a package is not enough to run it: an adapter receives
resolved credentials and may perform side effects, so only entry points named in the operator's
allowlist are loaded (`WAKECORE_PLUGINS=acme_crm,acme_ticket`, or `build(plugins=[...])`).

A plugin that fails to import, raises, returns something that is not an adapter, or claims an
id that is already taken is skipped with a warning; it never takes the kernel down and never
replaces a built-in adapter. Loading a tool does not authorise it: tasks still need a grant,
SystemPolicy must allow its capability, and the planner only sees granted tools.
"""
import logging
import os
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from typing import Any, Iterable, Optional

from wakecore.kernel.ports.action import ToolDescriptor
from wakecore.kernel.ports.observation import ConnectorDescriptor

log = logging.getLogger("wakecore.plugins")

CONNECTORS = "wakecore.connectors"
TOOLS = "wakecore.tools"
GROUPS = (CONNECTORS, TOOLS)
ENV = "WAKECORE_PLUGINS"


@dataclass(frozen=True)
class PluginContext:
    """What a factory may use. Deliberately small: no secrets store, no kernel services."""

    name: str
    clock: Any
    store: Any
    config: dict[str, Any] = field(default_factory=dict)   # this plugin's own settings (operator-supplied)
    ui_runtime: Any = None                                 # a UiRuntimeClient when one is configured


@dataclass(frozen=True)
class PluginInfo:
    group: str
    name: str
    value: str
    distribution: Optional[str]
    enabled: bool


@dataclass
class LoadResult:
    connectors: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, Any] = field(default_factory=dict)
    loaded: list[str] = field(default_factory=list)          # "group:name -> adapter id"
    errors: dict[str, str] = field(default_factory=dict)     # entry point name -> reason
    missing: list[str] = field(default_factory=list)         # allowlisted but not installed


def allowlist(value: Optional[Iterable[str]] = None) -> frozenset[str]:
    """Explicit names only; there is no wildcard (installing a package must not enable it)."""
    if value is None:
        value = os.environ.get(ENV, "").split(",")
    return frozenset(n.strip() for n in value if n and n.strip())


def _entry_points(group: str) -> list[EntryPoint]:
    return sorted(entry_points(group=group), key=lambda e: e.name)


def discover(allowed: Optional[Iterable[str]] = None) -> list[PluginInfo]:
    """Installed plugins, without importing any of them."""
    allow = allowlist(allowed)
    out = []
    for group in GROUPS:
        for ep in _entry_points(group):
            dist = getattr(ep, "dist", None)
            out.append(PluginInfo(group, ep.name, ep.value, dist.name if dist is not None else None,
                                  ep.name in allow))
    return out


def _check(group: str, adapter: Any) -> str:
    """-> adapter id, or raises TypeError. Shape only; `wakecore.testing.conformance` goes further."""
    d = getattr(adapter, "descriptor", None)
    if group == CONNECTORS:
        if not isinstance(d, ConnectorDescriptor) or not callable(getattr(adapter, "fetch", None)):
            raise TypeError("a connector needs a ConnectorDescriptor `descriptor` and fetch()")
        return d.connector_id
    if not isinstance(d, ToolDescriptor) or not callable(getattr(adapter, "execute", None)) \
            or not callable(getattr(adapter, "reconcile", None)):
        raise TypeError("a tool needs a ToolDescriptor `descriptor`, execute() and reconcile()")
    return d.tool_id


def load(*, clock: Any, store: Any, allowed: Optional[Iterable[str]] = None,
         config: Optional[dict[str, dict[str, Any]]] = None, ui_runtime: Any = None,
         taken_connectors: Iterable[str] = (), taken_tools: Iterable[str] = ()) -> LoadResult:
    allow = allowlist(allowed)
    result = LoadResult()
    if not allow:
        return result
    taken = {CONNECTORS: set(taken_connectors), TOOLS: set(taken_tools)}
    seen: set[str] = set()
    for group in GROUPS:
        target = result.connectors if group == CONNECTORS else result.tools
        for ep in _entry_points(group):
            if ep.name not in allow:
                continue
            seen.add(ep.name)
            label = f"{group}:{ep.name}"
            try:
                factory = ep.load()
                adapter = factory(PluginContext(name=ep.name, clock=clock, store=store,
                                                config=dict((config or {}).get(ep.name) or {}), ui_runtime=ui_runtime))
                adapter_id = _check(group, adapter)
            except Exception as exc:  # noqa: BLE001 - one bad plugin must not stop the kernel
                result.errors[ep.name] = f"{type(exc).__name__}: {exc}"[:500]
                log.warning("plugin %s skipped: %s", label, result.errors[ep.name])
                continue
            if adapter_id in taken[group]:
                result.errors[ep.name] = f"id {adapter_id!r} is already registered"
                log.warning("plugin %s skipped: %s", label, result.errors[ep.name])
                continue
            taken[group].add(adapter_id)
            target[adapter_id] = adapter
            result.loaded.append(f"{label} -> {adapter_id}")
    result.missing = sorted(allow - seen)
    for name in result.missing:
        log.warning("plugin %s is allowlisted but not installed", name)
    return result
