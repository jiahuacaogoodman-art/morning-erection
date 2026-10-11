"""Opt-in local course fixture + actual Chromium action. Never used for a real website."""
import html
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from wakecore.kernel.domain.enums import SideEffectClass
from wakecore.kernel.ports.action import ActionResult, ReconcileResult, ToolDescriptor

from ..browser.observer import observe
from ..journal import sent_mutations

EXTRACTOR = {"table": {"selector": "#courses"}, "key_attr": "data-course-id", "ready": "#courses",
             "columns": [{"field": "name", "headers": ["课程"]},
                         {"field": "seats", "headers": ["余量"], "type": "int"},
                         {"field": "selected", "headers": ["已选"], "type": "bool", "true": ["是"], "false": ["否"]}],
             "login": {"url_contains": ["/login"], "title_contains": ["重新登录"]}}


class CourseDemo:
    extractor = EXTRACTOR
    def __init__(self, path, port=8791):
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.lock = threading.RLock()
        self.csrf = secrets.token_urlsafe(24)
        self.db.execute("CREATE TABLE IF NOT EXISTS course (id INTEGER PRIMARY KEY, seats INTEGER, selected INTEGER, auth INTEGER, mode TEXT, submits INTEGER)")
        self.db.execute("INSERT OR IGNORE INTO course VALUES (1,0,0,1,'okay',0)")
        demo = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, text, **headers):
                data = text.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k.replace("_", "-"), v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                seats, selected, auth, mode, _ = demo.state()
                if mode == "429":
                    return self.reply(429, "<h1>请求过于频繁</h1>", Retry_After="15")
                if mode == "503":
                    return self.reply(503, "<h1>页面暂时不可用</h1>", Retry_After="10")
                if not auth and self.path != "/login":
                    self.send_response(302)
                    self.send_header("Location", "/login")
                    self.end_headers()
                    return
                if self.path == "/login":
                    return self.reply(200, demo.shell("重新登录", '<h1>演示登录已失效</h1><p>这是本地测试页面，不需要账号密码。</p><form method="post" action="/login"><button>重新进入演示</button></form>'))
                if mode == "schema":
                    return self.reply(200, demo.shell("页面已改版", "<h1>课程页面已经更换结构</h1>"))
                disabled = "disabled" if seats <= 0 or selected else ""
                text = f'<h1>可选课程</h1><p>真实浏览器读取的本地演示页面</p><table id="courses"><thead><tr><th>课程</th><th>余量</th><th>已选</th><th>操作</th></tr></thead><tbody><tr data-course-id="SE"><td>软件工程</td><td><span id="seats" data-label="课程余量">{seats}</span></td><td>{"是" if selected else "否"}</td><td><form method="post" action="/enroll"><input type="hidden" name="csrf" value="{html.escape(demo.csrf)}"><button id="enroll" {disabled}>选择课程</button></form></td></tr></tbody></table><p class="foot">仅用于本地验证 · 不连接真实教务系统</p>'
                return self.reply(200, demo.shell("教务系统 · 本地演示", text))

            def do_POST(self):  # noqa: N802
                if self.path == "/login":
                    demo.control({"auth": True, "mode": "okay"})
                elif self.path == "/enroll":
                    data = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
                    if data.get("csrf") != [demo.csrf]:
                        return self.reply(403, "CSRF 校验失败")
                    with demo.lock:
                        seats, selected, auth, _, _ = demo.state()
                        if not auth:
                            return self.reply(401, "需要登录")
                        if seats < 1 and not selected:
                            return self.reply(409, "名额已被占用")
                        demo.db.execute("UPDATE course SET seats=CASE WHEN selected=0 THEN seats-1 ELSE seats END, selected=1, submits=submits+1 WHERE id=1")
                else:
                    return self.reply(404, "未找到页面")
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.http = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}/"
        self.origin = self.url.rstrip("/")
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def state(self):
        with self.lock:
            return self.db.execute("SELECT seats,selected,auth,mode,submits FROM course WHERE id=1").fetchone()

    def control(self, data):
        with self.lock:
            if data.get("reset"):
                self.db.execute("UPDATE course SET seats=0, selected=0, auth=1, mode='okay', submits=0 WHERE id=1")
            if "seats" in data:
                value = max(0, min(100, int(data["seats"])))
                self.db.execute("UPDATE course SET seats=? WHERE id=1", [value])
            if "auth" in data:
                self.db.execute("UPDATE course SET auth=? WHERE id=1", [int(bool(data["auth"]))])
            if data.get("mode") in ("okay", "429", "503", "schema"):
                self.db.execute("UPDATE course SET mode=? WHERE id=1", [data["mode"]])

    def recipe(self, record_id):
        if record_id != "SE":
            raise ValueError("the demo can only enrol SE")
        return {"tool_id": "demo.course.enroll", "capability": "demo.course.enroll", "data_egress": [],
                "payload": {"goal": "在本地演示教务页面选择软件工程，并确认已选状态", "start_url": self.url}}

    def shell(self, title, body):
        return f'<!doctype html><html lang="zh-CN"><head><title>{title}</title><meta name="viewport" content="width=device-width, initial-scale=1"><style>body{{font:16px system-ui;max-width:820px;margin:80px auto;padding:24px;color:#24251f;background:#fafaf7}}h1{{font-size:30px}}table{{width:100%;border-collapse:collapse;margin:36px 0}}th,td{{text-align:left;padding:18px;border-bottom:1px solid #ddd}}button{{padding:12px 20px;border:0;border-radius:12px;background:#24251f;color:white;cursor:pointer}}button:disabled{{opacity:.35}}p,.foot{{color:#777}}</style></head><body>{body}</body></html>'

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.db.close()


class CourseDemoTool:
    descriptor = ToolDescriptor(
        tool_id="demo.course.enroll", tool_version="1", capability_type="demo.course.enroll",
        input_schema={"type": "object", "required": ["goal", "start_url"],
                      "properties": {"goal": {"type": "string"}, "start_url": {"type": "string"}}},
        output_schema={"type": "object"}, allowed_resource_kinds=("origin",),
        side_effect_class=SideEffectClass.EXTERNAL_WRITE, supports_idempotency=False,
        idempotency_retention_seconds=0, supports_reconciliation=True, confirmation_semantics="re_observation",
        max_duration_seconds=30, url_fields=("start_url",), credential="source_binding")

    def __init__(self, demo, runtime):
        self.demo, self.runtime = demo, runtime

    def _read(self, worker):
        return observe(worker, {}, url=self.demo.url, origins=[self.demo.origin], extractor=EXTRACTOR,
                       timeout_ms=5000, ready_timeout_ms=1000)

    def _held(self, result):
        return result.get("outcome") == "SUCCESS" and result.get("records", {}).get("SE", {}).get("selected") is True

    def execute(self, request):
        if request.payload["start_url"] != self.demo.url or request.resource_scope.get("origins") != [self.demo.origin]:
            return ActionResult("failed_no_effect", error="demo_origin_only")

        def run(worker):
            pre = self._read(worker)
            if self._held(pre):
                return ActionResult("confirmed", receipt={"verified": True, "already_selected": True})
            if pre.get("outcome") != "SUCCESS":
                return ActionResult("failed_no_effect", error="demo_not_ready")
            journal, key = self.runtime.journal, request.effect_key
            if sent_mutations(journal.get(key)):
                return ActionResult("unknown", error="previous_submit_not_verified")
            journal.begin(key, request.attempt_id, request.secret, request_digest=request.payload_digest)
            context, guard = worker.context, worker.guard
            guard.enter("act", frozenset([self.demo.origin]), journal=journal, key=key)
            page = guard.own(context.new_page())
            error = None
            try:
                page.goto(self.demo.url, timeout=5000)
                page.locator("#enroll").click(timeout=3000)
                page.wait_for_url(self.demo.url, timeout=5000)
            except Exception:
                error = "demo_submit_incomplete"
            finally:
                guard.leave()
                guard.disown(page)
                guard.close_strays(context)
            post = self._read(worker)
            status = "confirmed" if self._held(post) else "unknown" if sent_mutations(journal.get(key)) else "failed_no_effect"
            journal.update(key, status="finished", finished_at=time.time(), result={"status": status})
            return ActionResult(status, provider_request_id=key, receipt={"verified": status == "confirmed",
                                "mutating_requests": sent_mutations(journal.get(key)), "records": post.get("records")}, error=error)
        return self.runtime._on(request.secret, run, 30)

    def reconcile(self, request):
        result = self.runtime._on(request.secret, self._read, 15)
        if self._held(result):
            return ReconcileResult("confirmed", evidence={"verified": True})
        if not sent_mutations(self.runtime.journal.get(request.effect_key)):
            return ReconcileResult("no_effect")
        return ReconcileResult("still_unknown")
