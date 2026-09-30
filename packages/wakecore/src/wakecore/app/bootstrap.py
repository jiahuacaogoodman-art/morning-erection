"""Composition root: the only place that wires adapters into a KernelContext."""
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from wakecore import plugins as plugin_loader
from wakecore.adapters.clock import SystemClock, UuidIds
from wakecore.adapters.models.scripted import ScriptedModel
from wakecore.adapters.secrets.static import StaticSecrets
from wakecore.adapters.sources.offline_grades import OfflineGradesSource
from wakecore.adapters.tools.fake_external import FakeEmailProvider
from wakecore.adapters.tools.inbox import InboxTool
from wakecore.adapters.ui_runtime.client import UiRuntimeClient
from wakecore.kernel.context import KernelConfig, KernelContext, SystemPolicy
from wakecore.kernel.ports.faults import NoFaults


def open_store(url: Optional[str], clock: Any) -> Any:
    """`sqlite:///path.db` (dev only) or `postgresql://...` (production, M2)."""
    url = url or os.environ.get("WAKECORE_DB", "sqlite:///wakecore-dev.db")
    if url.startswith("sqlite:///"):
        from wakecore.adapters.sqlite_dev.store import SqliteStore

        store = SqliteStore(url[len("sqlite:///"):], clock)
    elif url.startswith(("postgresql://", "postgres://")):
        from wakecore.adapters.postgres.store import PostgresStore

        store = PostgresStore(url, clock=None)
    else:
        raise ValueError("unsupported database url")
    store.migrate()
    return store


@dataclass
class Kernel:
    ctx: KernelContext
    grades: OfflineGradesSource
    model: Optional[ScriptedModel]
    email: FakeEmailProvider
    inbox: InboxTool
    secrets: StaticSecrets = field(default_factory=StaticSecrets)
    web: Any = None       # PlaywrightWebSource, when a UI Runtime is configured
    browser: Any = None   # BrowserCuaTool, likewise
    desktop: Any = None   # DesktopAppSource, when a Desktop Runtime is configured
    desktop_hand: Any = None   # DesktopCuaTool, likewise
    plugins: Any = None   # wakecore.plugins.LoadResult (allowlisted entry points)


def build(*, store: Any = None, db_url: Optional[str] = None, clock: Any = None, ids: Any = None,
          with_model: bool = True, faults: Any = None, config: Optional[KernelConfig] = None,
          system_policy: Optional[SystemPolicy] = None, grades: Optional[OfflineGradesSource] = None,
          ui_runtime_url: Optional[str] = None, ui_runtime_token: Optional[str] = None,
          browser_settle_seconds: int = 120, browser_model_egress: Optional[str] = None,
          desktop_runtime_url: Optional[str] = None, desktop_runtime_token: Optional[str] = None,
          desktop_model_egress: Optional[str] = None, plugins: Optional[list[str]] = None,
          plugin_config: Optional[dict[str, dict[str, Any]]] = None) -> Kernel:
    """`ui_runtime_url` (or WAKECORE_UI_RUNTIME_URL) enables the browser eye/hand (V0.3).

    Enabling it is the operator's decision, so the default SystemPolicy then also allows
    web.observe, browser.operate and the Computer Use model egress. Tasks still need a grant
    and an approval for each browser action; an explicit `system_policy` is used as given.

    `browser_model_egress` (or WAKECORE_UI_MODEL_EGRESS) is who receives the screenshots:
    "model:openai-computer-use" (default) or "model:openai-compatible:<host>" for a relay, exactly
    as the sidecar reports it in `wakecore ui health`. The runtime refuses acts if they differ.

    `desktop_runtime_url` (or WAKECORE_DESKTOP_RUNTIME_URL, token WAKECORE_DESKTOP_RUNTIME_TOKEN)
    enables the macOS app eye/hand (desktop.macos, desktop.operate) the same way: the default
    SystemPolicy then allows desktop.observe, desktop.operate and `desktop_model_egress` (or
    WAKECORE_DESKTOP_MODEL_EGRESS, default "model:openai-computer-use").

    `plugins` (or WAKECORE_PLUGINS) names the entry-point plugins to load (wakecore.plugins).
    They are added after the built-in adapters and can never replace one. SystemPolicy is not
    widened for them: the operator allows a plugin's capability explicitly.
    """
    clock = clock or SystemClock()
    explicit_policy = system_policy is not None
    store = store or open_store(db_url, clock)
    secrets = StaticSecrets()
    grades = grades or OfflineGradesSource()
    inbox = InboxTool(store)
    email = FakeEmailProvider()
    model = ScriptedModel() if with_model else None
    connectors: dict[str, Any] = {grades.descriptor.connector_id: grades}
    tools: dict[str, Any] = {inbox.descriptor.tool_id: inbox, email.descriptor.tool_id: email}
    web = browser = client = None
    ui_runtime_url = ui_runtime_url or os.environ.get("WAKECORE_UI_RUNTIME_URL")
    if ui_runtime_url:
        from wakecore.adapters.sources.playwright_web import PlaywrightWebSource
        from wakecore.adapters.tools.browser_cua import MODEL_EGRESS, BrowserCuaTool
        model_egress = browser_model_egress or os.environ.get("WAKECORE_UI_MODEL_EGRESS") or MODEL_EGRESS

        client = UiRuntimeClient(ui_runtime_url, token=ui_runtime_token or os.environ.get("WAKECORE_UI_RUNTIME_TOKEN", ""))
        web, browser = PlaywrightWebSource(client), BrowserCuaTool(client, clock, settle_seconds=browser_settle_seconds,
                                                                  model_egress=model_egress)
        connectors[web.descriptor.connector_id] = web
        tools[browser.descriptor.tool_id] = browser
        if system_policy is None:
            base = SystemPolicy()
            system_policy = SystemPolicy(
                allowed_capabilities=base.allowed_capabilities | {"web.observe", "browser.operate"},
                allowed_data_egress=base.allowed_data_egress | {model_egress},
                auto_approve_capabilities=base.auto_approve_capabilities)
    desktop = desktop_hand = None
    desktop_runtime_url = desktop_runtime_url or os.environ.get("WAKECORE_DESKTOP_RUNTIME_URL")
    if desktop_runtime_url:
        from wakecore.adapters.sources.desktop_app import DesktopAppSource
        from wakecore.adapters.tools.browser_cua import MODEL_EGRESS
        from wakecore.adapters.tools.desktop_cua import DesktopCuaTool
        from wakecore.adapters.ui_runtime.desktop_client import DesktopRuntimeClient
        egress = desktop_model_egress or os.environ.get("WAKECORE_DESKTOP_MODEL_EGRESS") or MODEL_EGRESS
        dclient = DesktopRuntimeClient(desktop_runtime_url, token=desktop_runtime_token
                                       or os.environ.get("WAKECORE_DESKTOP_RUNTIME_TOKEN", ""))
        desktop, desktop_hand = DesktopAppSource(dclient), DesktopCuaTool(dclient, clock, model_egress=egress)
        connectors[desktop.descriptor.connector_id] = desktop
        tools[desktop_hand.descriptor.tool_id] = desktop_hand
        if not explicit_policy:   # widen only the default policy (maybe already widened for the browser)
            base = system_policy or SystemPolicy()
            system_policy = SystemPolicy(
                allowed_capabilities=base.allowed_capabilities | {"desktop.observe", "desktop.operate"},
                allowed_data_egress=base.allowed_data_egress | {egress},
                auto_approve_capabilities=base.auto_approve_capabilities)
    loaded = plugin_loader.load(clock=clock, store=store, allowed=plugins, config=plugin_config, ui_runtime=client,
                                taken_connectors=connectors, taken_tools=tools)
    connectors.update(loaded.connectors)
    tools.update(loaded.tools)
    ctx = KernelContext(
        store=store, clock=clock, ids=ids or UuidIds(), connectors=connectors, tools=tools, reasoning=model,
        secrets=secrets, system_policy=system_policy or SystemPolicy(), faults=faults or NoFaults(),
        config=config or KernelConfig())
    return Kernel(ctx=ctx, grades=grades, model=model, email=email, inbox=inbox, secrets=secrets, web=web,
                  browser=browser, desktop=desktop, desktop_hand=desktop_hand, plugins=loaded)
