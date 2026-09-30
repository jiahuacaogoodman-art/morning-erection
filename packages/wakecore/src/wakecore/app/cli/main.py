"""`wakecore` command line: init-db, demo, serve-api, worker, replay, task show/timeline, ui, plugins, openapi.

Secrets and API tokens come from a JSON file named by WAKECORE_SECRETS_FILE, never from
command-line arguments (they would leak into shell history and process listings):

    {"secrets": {"local_user": {"sec_school": "...", "sec_ingress": "..."}},
     "tokens":  {"<bearer token>": {"tenant_id": "local_user", "subject": "user:alice"}}}
"""
import argparse
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone
from importlib import resources
from typing import Any, Optional

from wakecore import __version__
from wakecore.app.bootstrap import Kernel, build, open_store
from wakecore.kernel.service import KernelService, Principal, view


def _load_secrets_file() -> dict[str, Any]:
    path = os.environ.get("WAKECORE_SECRETS_FILE")
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _kernel(args: argparse.Namespace) -> Kernel:
    k = build(db_url=args.db)
    for tenant, refs in (_load_secrets_file().get("secrets") or {}).items():
        for ref, value in refs.items():
            k.secrets.put(tenant, ref, value)
    return k


def _print(obj: Any) -> None:
    print(json.dumps(view(obj), ensure_ascii=False, indent=2, sort_keys=True))


# ---------------------------------------------------------------------- commands

def cmd_init_db(args: argparse.Namespace) -> int:
    from wakecore.adapters.clock import SystemClock

    open_store(args.db, SystemClock()).close()
    print(f"数据库已初始化：{args.db or os.environ.get('WAKECORE_DB', 'sqlite:///wakecore-dev.db')}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Offline grades demo on a fake clock: nothing leaves the machine, no model is called."""
    from wakecore.adapters.clock import FakeClock, SequentialIds
    from wakecore.app.worker.loop import run_until_idle
    from wakecore.kernel.domain.model import InboxMessage
    from wakecore.kernel.locks import tx
    from wakecore.kernel.replay import replay_task

    if args.spec:
        with open(args.spec, encoding="utf-8") as fh:
            spec = json.load(fh)
    else:
        spec = json.loads(resources.files("wakecore.app.cli").joinpath("grades_demo.json").read_text(encoding="utf-8"))

    clock = FakeClock(datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc))
    db = args.db or "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="wakecore-demo-"), "demo.db")
    k = build(db_url=db, clock=clock, ids=SequentialIds(), with_model=False)
    k.secrets.put("local_user", "sec_school", "session-cookie-demo")
    k.secrets.put("local_user", "sec_ingress", "ingress-hmac-demo")
    k.grades.courses = {"PHARM": {"name": "药理学", "score": None},
                        "PATHOPHYS": {"name": "病理生理学", "score": None}}
    svc = KernelService(k.ctx)
    me = Principal("local_user", "user:alice")

    def inbox() -> list[InboxMessage]:
        with tx(k.ctx.store) as repo:
            return repo.find(InboxMessage, {"tenant_id": "local_user"}, order_by=("created_at", "message_id"))

    def tick(label: str, seconds: int = 300) -> None:
        clock.advance(seconds)
        run_until_idle(k.ctx, "demo-worker", max_rounds=50)
        t = svc.get_task(me, spec["task_id"])
        print(f"[{clock.utc_now():%m-%d %H:%M}] {label} → 状态 {t['lifecycle']}：{t['explanation']}")

    print("== WakeCore 离线成绩演示（假时钟，无模型、无外部发送）==")
    svc.register_grant(me, grant_ref=spec["authority"]["grant_ref"], capabilities=["grades.read", "inbox.notify_self"],
                       data_egress=[], resource_scope={"semester": "fall_demo"})
    svc.register_binding(me, source_ref=spec["source"]["binding_ref"], connector_id="offline.grades",
                         source_uri="urn:wakecore:source:school-demo", resource_scope={"semester": "fall_demo"},
                         capabilities=["grades.read"], secret_ref="sec_school", ingress_secret_ref="sec_ingress")
    created = svc.create_task(me, spec, idem_key="demo-create").body
    print(f"草稿已创建：{created['task_id']} v{created['spec_version']} {created['spec_digest'][:19]}…")
    svc.activate_task(me, created["task_id"], spec_version=created["spec_version"],
                      spec_digest=created["spec_digest"], idem_key="demo-activate")
    tick("首次检查（尚未出分）", 0)
    k.grades.set_score("PHARM", 88)
    tick("药理学出分")
    tick("无变化的一次检查")
    k.grades.set_score("PATHOPHYS", 91)
    tick("病理生理学出分")
    print("\n站内信：")
    for m in inbox():
        print(f"  · {m.title}：{m.body}")
    report = replay_task(k.ctx.store, "local_user", spec["task_id"])
    s = report.summary()
    print(f"\n确定性回放：{s['matches']}/{s['replayed']} 一致，外部写入 {s['external_writes']} 次")
    print(f"数据库：{db}")
    return 0 if not report.mismatches else 1


def cmd_serve_api(args: argparse.Namespace) -> int:
    from wsgiref.simple_server import make_server

    from wakecore.app.api.wsgi import TokenAuth, WakeCoreAPI, token_digest

    k = _kernel(args)
    tokens = _load_secrets_file().get("tokens") or {}
    if not tokens:
        print("未配置 API 令牌（WAKECORE_SECRETS_FILE 中的 tokens），拒绝启动", file=sys.stderr)
        return 2
    auth = TokenAuth({token_digest(t): Principal(p["tenant_id"], p["subject"]) for t, p in tokens.items()})
    app = WakeCoreAPI(KernelService(k.ctx), auth)
    stop = threading.Event()
    if args.with_worker:
        from wakecore.app.worker.loop import serve

        threading.Thread(target=serve, args=(k.ctx, "api-embedded-worker"), kwargs={"stop": stop},
                         daemon=True).start()
    with make_server(args.host, args.port, app) as httpd:
        print(f"WakeCore API 监听 http://{args.host}:{args.port}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from wakecore.app.worker.loop import run_until_idle, serve

    k = _kernel(args)
    if args.once:
        done = run_until_idle(k.ctx, args.worker_id, max_rounds=args.max_rounds)
        print(f"处理了 {len(done)} 个工作单元")
        return 0
    stop = threading.Event()
    try:
        serve(k.ctx, args.worker_id, idle_sleep=args.idle_sleep, stop=stop)
    except KeyboardInterrupt:
        stop.set()
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from wakecore.kernel.replay import replay_task

    k = _kernel(args)
    report = replay_task(k.ctx.store, args.tenant, args.task_id)
    if args.verbose:
        _print({"summary": report.summary(), "items": report.items, "skipped": report.skipped})
    else:
        _print(report.summary())
    return 0 if not report.mismatches else 1


def cmd_task(args: argparse.Namespace) -> int:
    k = _kernel(args)
    svc = KernelService(k.ctx)
    p = Principal(args.tenant, args.principal)
    _print(svc.get_task(p, args.task_id) if args.action == "show" else svc.timeline(p, args.task_id))
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """Talk to a UI Runtime (WAKECORE_UI_RUNTIME_URL / _TOKEN). `check-extractor` also works offline."""
    from wakecore.adapters.ui_runtime.client import UiRuntimeClient, UiRuntimeError

    url = args.runtime or os.environ.get("WAKECORE_UI_RUNTIME_URL", "")
    client = UiRuntimeClient(url, token=os.environ.get("WAKECORE_UI_RUNTIME_TOKEN", "")) if url else None
    if args.action == "check-extractor":
        if not args.file:
            print("usage: wakecore ui check-extractor FILE.json", file=sys.stderr)
            return 2
        with open(args.file, encoding="utf-8") as fh:
            cfg = json.load(fh)
        if isinstance(cfg, dict) and "extractor" in cfg and "columns" not in cfg:   # a whole resource_scope
            cfg = cfg["extractor"]
        if client is None:
            from wakecore.protocol.jsonschema_lite import SchemaError, registry
            try:
                registry().validate(cfg, "extractor.v1.schema.json")
            except SchemaError as e:
                _print({"ok": False, "checked_by": "schema", "error": {"code": "extractor_invalid", "message": str(e)}})
                return 1
            _print({"ok": True, "checked_by": "schema",
                    "note": "schema only; set WAKECORE_UI_RUNTIME_URL for the runtime's full check"})
            return 0
        try:
            r = client.validate_extractor(cfg)
        except UiRuntimeError as e:
            _print({"ok": False, "error": {"code": getattr(e, "code", type(e).__name__), "message": str(e)}})
            return 1
        _print({**r, "checked_by": "runtime"})
        return 0 if r.get("ok") else 1
    if client is None:
        print("set WAKECORE_UI_RUNTIME_URL or pass --runtime", file=sys.stderr)
        return 2
    try:
        r = {"health": client.health, "capabilities": client.capabilities, "sessions": client.sessions}[args.action]()
        if args.action == "health" and r.get("model_egress"):
            from wakecore.adapters.tools.browser_cua import MODEL_EGRESS
            mine = os.environ.get("WAKECORE_UI_MODEL_EGRESS") or MODEL_EGRESS
            r = {**r, "kernel_model_egress": mine, "model_egress_matches": mine == r["model_egress"]}
            if mine != r["model_egress"]:
                print(f"warning: browser acts will be refused (model_egress_mismatch): set "
                      f"WAKECORE_UI_MODEL_EGRESS={r['model_egress']} and grant that egress", file=sys.stderr)
        _print(r)
    except UiRuntimeError as e:
        _print({"ok": False, "error": {"code": getattr(e, "code", type(e).__name__), "message": str(e)}})
        return 1
    return 0


def cmd_plugins(args: argparse.Namespace) -> int:
    """Installed entry-point plugins and whether WAKECORE_PLUGINS enables them. Imports none of them."""
    from wakecore.plugins import ENV, allowlist, discover

    found = discover()
    installed = {p.name for p in found}
    _print({"allowlist_env": ENV,
            "plugins": [{"group": p.group, "name": p.name, "entry_point": p.value, "distribution": p.distribution,
                         "enabled": p.enabled} for p in found],
            "allowlisted_but_missing": sorted(allowlist() - installed)})
    return 0


def kernel_openapi() -> dict:
    """OpenAPI document of the management/ingress API, generated from WakeCoreAPI's route table."""
    from wakecore.app.api.wsgi import TokenAuth, WakeCoreAPI
    from wakecore.protocol.openapi import kernel_api_openapi

    return kernel_api_openapi(WakeCoreAPI(None, TokenAuth({})).routes)  # type: ignore[arg-type]


def cmd_openapi(args: argparse.Namespace) -> int:
    from wakecore.protocol.openapi import dumps

    sys.stdout.write(dumps(kernel_openapi()))
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="wakecore", description="WakeCore Kernel")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--db", default=None, help="sqlite:///path.db 或 postgresql://…（默认读 WAKECORE_DB）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="创建/迁移数据库结构").set_defaults(fn=cmd_init_db)

    d = sub.add_parser("demo", help="离线成绩演示")
    d.add_argument("--spec", default=None)
    d.set_defaults(fn=cmd_demo)

    s = sub.add_parser("serve-api", help="启动管理与接入 API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--with-worker", action="store_true", help="同进程内再跑一个 worker（仅开发用）")
    s.set_defaults(fn=cmd_serve_api)

    w = sub.add_parser("worker", help="启动 worker")
    w.add_argument("--worker-id", default=f"worker-{os.getpid()}")
    w.add_argument("--once", action="store_true", help="处理到空闲后退出")
    w.add_argument("--max-rounds", type=int, default=1000)
    w.add_argument("--idle-sleep", type=float, default=1.0)
    w.set_defaults(fn=cmd_worker)

    r = sub.add_parser("replay", help="只读确定性回放")
    r.add_argument("task_id")
    r.add_argument("--tenant", default="local_user")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(fn=cmd_replay)

    t = sub.add_parser("task", help="查看任务")
    t.add_argument("action", choices=["show", "timeline"])
    t.add_argument("task_id")
    t.add_argument("--tenant", default="local_user")
    t.add_argument("--principal", default="user:alice")
    t.set_defaults(fn=cmd_task)

    u = sub.add_parser("ui", help="UI Runtime：check-extractor / health / capabilities / sessions")
    u.add_argument("action", choices=["check-extractor", "health", "capabilities", "sessions"])
    u.add_argument("file", nargs="?", help="check-extractor 的 extractor 或 resource_scope JSON 文件")
    u.add_argument("--runtime", default=None, help="UI Runtime 地址（默认读 WAKECORE_UI_RUNTIME_URL）")
    u.set_defaults(fn=cmd_ui)

    pl = sub.add_parser("plugins", help="列出已安装的插件（entry points）及是否在 WAKECORE_PLUGINS 白名单中")
    pl.add_argument("action", choices=["list"])
    pl.set_defaults(fn=cmd_plugins)

    sub.add_parser("openapi", help="打印管理/接入 API 的 OpenAPI 3.1 文档（由路由表生成）").set_defaults(fn=cmd_openapi)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = parser().parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
