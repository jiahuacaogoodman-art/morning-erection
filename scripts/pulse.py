"""Run the local Pulse application; --demo uses a real browser and a local course page."""
import argparse
import atexit
import fcntl
import os
import secrets
import sys
import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from wakecore.adapters.agent_runtime.native import NativePiError, NativePiHost
from wakecore.adapters.models.semantic import AgentInterpreter, SemanticTextClient
from wakecore.adapters.models.semantic_watch import SemanticWatchReasoner
from wakecore.adapters.models.watch_plan import WatchPlan
from wakecore.adapters.ui_runtime.client import UiRuntimeClient
from wakecore.app.bootstrap import build
from wakecore.app.pulse.gateways import GatewayError, GatewayStore
from wakecore.app.pulse.server import LocalPulseApp
from wakecore.app.worker.loop import serve

ROOT = Path(__file__).resolve().parents[1]


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="Morning Erection · Pulse")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--demo-port", type=int, default=8791)
    parser.add_argument("--state", type=Path, default=ROOT / ".pulse")
    parser.add_argument("--runtime-url", default=os.environ.get("WAKECORE_UI_RUNTIME_URL"))
    parser.add_argument("--model", default=os.environ.get("WAKECORE_AGENT_MODEL") or os.environ.get("WAKECORE_CU_MODEL") or "gpt-5.6-sol")
    parser.add_argument("--model-base-url", default=os.environ.get("OPENAI_BASE_URL"))
    parser.add_argument("--model-key-file", type=Path, default=os.environ.get("OPENAI_API_KEY_FILE"))
    parser.add_argument("--variant", default=os.environ.get("WAKECORE_CU_VARIANT"))
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--watch-only", action="store_true", help="run the persistent Watch service without Pi or frontend")
    parser.add_argument("--no-custom-modules", action="store_true", help="native Pi only; no Morning extensions or workers")
    parser.add_argument("--no-computer", action="store_true", help="disable the optional Computer module")
    parser.add_argument("--no-watch", action="store_true", help="disable the optional Watch module")
    args = parser.parse_args()
    args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.state.chmod(0o700)
    state_lock = (args.state / "service.lock").open("a+")
    os.fchmod(state_lock.fileno(), 0o600)
    try:
        fcntl.flock(state_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("这个数据目录已有服务运行。请连接现有服务，或使用独立的 --state 目录。")
    atexit.register(state_lock.close)
    # Reserve the console port before starting Pi, workers or sidecars.
    server = make_server("127.0.0.1", args.port, None, server_class=ThreadedServer, handler_class=QuietHandler)
    args.port = server.server_port
    atexit.register(server.server_close)
    runtime = runtime_http = demo = None
    token = os.environ.get("WAKECORE_UI_RUNTIME_TOKEN", "")
    key_file = args.model_key_file or ROOT / ".secrets" / "openai.key"
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if key_file.exists():
        if key_file.stat().st_mode & 0o077:
            parser.error("model key file must have mode 0600")
        api_key = key_file.read_text().strip()
    base_url = args.model_base_url
    if not base_url and (ROOT / ".secrets" / "openai.base_url").exists():
        base_url = (ROOT / ".secrets" / "openai.base_url").read_text().strip()
    base_url = base_url or "https://api.openai.com/v1"
    gateways = GatewayStore(args.state)
    # One-time migration of an existing configuration; a deleted Gateway never reappears.
    if not gateways.path.exists() and api_key and not args.demo:
        gateways.save({"name": "现有模型网关", "base_url": base_url, "api": "openai-responses", "model": args.model,
                       "request_format": "instructions", "api_key": api_key})
    try:
        active = gateways.profile()
        credential = gateways.read_credential(active["provider_id"]) or {}
        base_url, api_key, args.model = active["base_url"], credential.get("key", ""), active["model"]
        if active["api"] != "openai-responses":
            api_key = ""  # optional browser/semantic adapter, not Pi's native provider auth
    except GatewayError:
        if not args.demo:
            api_key = ""
    semantic_client = SemanticTextClient(base_url=base_url, api_key=api_key, model=args.model)
    interpreter = AgentInterpreter(semantic_client)
    if not args.no_custom_modules and (args.demo or not args.runtime_url):
        from wakecore_ui_runtime.cua.openai import ResponsesClient
        from wakecore_ui_runtime.server import Runtime, make_handler
        from wakecore_ui_runtime.testing.pulse_demo import CourseDemo, CourseDemoTool

        if args.demo:
            demo = CourseDemo(args.state / "demo.db", port=args.demo_port)
        token = secrets.token_urlsafe(32)
        # Test fixtures can opt into the local tool; production has no examples or seeding.
        variant = args.variant or ("ga" if semantic_client.endpoint_host == "api.openai.com" else "function")
        runtime = Runtime(str(args.state / "browser"), client=ResponsesClient(api_key="" if args.demo else api_key,
            base_url=base_url, model=args.model, variant=variant), token=token, trace=False)
        runtime_http = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runtime))
        threading.Thread(target=runtime_http.serve_forever, daemon=True).start()
        args.runtime_url = f"http://127.0.0.1:{runtime_http.server_address[1]}"
    client = UiRuntimeClient(args.runtime_url, token=token) if args.runtime_url else None
    egress = None
    if client:
        try:
            egress = client.health().get("model_egress")
        except Exception:
            pass  # the UI exposes an unavailable runtime; credentials are never printed
    kernel = build(db_url=f"sqlite:///{args.state / 'kernel.db'}", ui_runtime_url=args.runtime_url,
                   ui_runtime_token=token, browser_model_egress=egress, with_model=False)
    kernel.ctx.reasoning = SemanticWatchReasoner(kernel.ctx.store, semantic_client) if api_key else WatchPlan(kernel.ctx.store)
    policy = kernel.ctx.system_policy
    kernel.ctx.system_policy = replace(policy, allowed_data_egress=policy.allowed_data_egress | {
        "model:local-watch-plan", semantic_client.data_egress_target})
    if demo:
        tool = CourseDemoTool(demo, runtime)
        kernel.ctx.tools[tool.descriptor.tool_id] = tool
        kernel.ctx.system_policy = replace(kernel.ctx.system_policy,
            allowed_capabilities=policy.allowed_capabilities | {"demo.course.enroll"})
    coding_runtime = None if args.watch_only else NativePiHost(runtime_path=ROOT / "apps" / "agent-runtime" / "native-bootstrap.mjs",
        workspace=args.workspace, state_dir=args.state, gateways=gateways)
    app = LocalPulseApp(kernel, client=client, demo=demo, interpreter=interpreter, native_host=coding_runtime, gateways=gateways, dist=None if args.watch_only else ROOT / "apps" / "pulse" / "dist",
                       state_dir=args.state, platform_enabled=not args.no_custom_modules and not args.demo,
                       allowed_hosts=[f"{host}:{port}" for host in ("127.0.0.1", "localhost") for port in (args.port, 5173)])

    def gateway_changed():
        # Browser extensions adopt the selected service only for future work. Existing grants
        # still bind their original egress and fail closed if the destination changed.
        try:
            selected = gateways.profile()
            stored = gateways.read_credential(selected["provider_id"]) or {}
            address, key, model = selected["base_url"], stored.get("key", ""), selected["model"]
            if selected["api"] != "openai-responses":
                key = ""  # the optional browser adapter requires the Responses protocol
        except GatewayError:
            address, key, model = base_url, "", args.model
        semantic_client.__init__(base_url=address, api_key=key, model=model)
        kernel.ctx.system_policy = replace(kernel.ctx.system_policy, allowed_data_egress=kernel.ctx.system_policy.allowed_data_egress | {semantic_client.data_egress_target})
        interpreter.client = semantic_client
        kernel.ctx.reasoning = SemanticWatchReasoner(kernel.ctx.store, semantic_client) if key else WatchPlan(kernel.ctx.store)
        if app.platform and hasattr(kernel.ctx.reasoning, "visual_resolver"):
            kernel.ctx.reasoning.visual_resolver = app.platform.source.semantic_images
        if runtime:
            runtime.client = ResponsesClient(api_key=key, base_url=address, model=model,
                variant=args.variant or ("ga" if semantic_client.endpoint_host == "api.openai.com" else "function"))
        app.api._runtime_cache = (0, {})

    app.api.gateway_changed = gateway_changed
    if coding_runtime and (any((args.state / "pi" / "sessions").glob("*.jsonl")) or (args.state / "pi/selected-session.json").exists()):
        def restore_agent():
            try:
                coding_runtime.control("start_agent", timeout=120)
            except NativePiError as exc:
                coding_runtime.error = str(exc)
        threading.Thread(target=restore_agent, daemon=True).start()
    stop = threading.Event()
    worker = None
    if app.platform:
        if args.no_watch:
            app.platform.modules["watch"] = False
        if args.no_computer:
            app.platform.modules["computer"] = False
        app.platform.start()
    elif not args.no_custom_modules:
        worker = threading.Thread(target=serve, args=(kernel.ctx, "pulse-local"), kwargs={"stop": stop, "idle_sleep": .3}, daemon=True)
        worker.start()
    server.set_app(app)
    with server:
        print(f"Pulse http://127.0.0.1:{args.port}" + (f" · 本地课程页面 {demo.url}" if demo else ""), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            if worker:
                worker.join(timeout=30)
            if app.platform:
                app.platform.close()
            if coding_runtime:
                coding_runtime.close()
            if runtime_http:
                runtime_http.shutdown()
            if runtime:
                runtime.human_login.close_all()
                runtime.sessions.close_all()
            if demo:
                demo.close()
            kernel.ctx.store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
