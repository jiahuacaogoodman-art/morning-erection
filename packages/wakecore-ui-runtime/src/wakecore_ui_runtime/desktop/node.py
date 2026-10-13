"""Model-free Computer node. Pi decides; this process observes and sends one approved step.

The MCP bridge and write-ahead journal are shared with the existing desktop runtime.
No model credentials, shell execution, or browser session are needed by this service.
"""
import argparse
import base64
import ctypes
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import shlex
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from wakecore.protocol.computer_actions import SCHEMAS, validate_action

from ..journal import Journal
from .loop import BUNDLE_ID, SYSTEM_KEYS, normalise_key
from .mcp import McpBridge
from .tree import TreeError, parse

PROTOCOL = "morning.computer-node/1"
ACTION_FIELDS = {
    "click": {"element_index", "x", "y", "click_count", "mouse_button"},
    "drag": {"from_x", "from_y", "to_x", "to_y"},
    "scroll": {"element_index", "direction", "pages"},
    "press_key": {"key"}, "type_text": {"text"}, "set_value": {"element_index", "value"},
    "select_text": {"element_index", "text", "prefix", "suffix", "selection"},
    "perform_secondary_action": {"element_index", "action"},
}


# Full access still leaves credential managers out unless the person names them explicitly.
DEFAULT_EXCLUDED = ["com.apple.keychainaccess", "com.1password.1password", "com.agilebits.onepassword7", "com.bitwarden.desktop", "com.apple.Passwords"]
APP_LINE = re.compile(r"^(.*?) — .*? — ([\w.-]+)(?: \[(.*)\])?$")


def parse_apps(text):
    apps = {}
    for line in text.splitlines():
        match = APP_LINE.match(line.strip())
        if match and match[2] not in apps:
            flags = (match[3] or "").split(", ")
            apps[match[2]] = {"id": match[2], "name": match[1], "frontmost": "frontmost" in flags, "running": "running" in flags}
    return list(apps.values())


class ComputerError(ValueError):
    def __init__(self, code, message, status=422):
        super().__init__(message)
        self.code, self.status = code, status


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def write_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    with open(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def default_bridge_command():
    configured = os.environ.get("WAKECORE_DESKTOP_MCP_COMMAND")
    if configured:
        return [os.path.expanduser(part) for part in shlex.split(configured)]
    binary = shutil.which("codex-computer-use-mcp")
    if binary:
        return [binary]
    source = Path.home() / "src/codex-computer-use-mcp/dist/mcp-server.js"
    # shutil.which("node") fails inside bundled/sandboxed environments where PATH
    # doesn't include the user's local node install. Fall back to known locations.
    node = (shutil.which("node") or
            next((p for p in [
                str(Path.home() / ".local/node/bin/node"),
                "/usr/local/bin/node",
                "/opt/homebrew/bin/node",
                "/usr/bin/node",
            ] if Path(p).exists()), "node"))
    return [node, str(source)]


def screen_permission():
    if sys.platform != "darwin":
        return None
    try:
        library = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        library.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(library.CGPreflightScreenCaptureAccess())
    except (OSError, AttributeError):
        return None


def accessibility_permission(prompt=False):
    """Check AX permission; if prompt=True, trigger the macOS dialog (for __main__ / explicit user request only)."""
    if sys.platform != "darwin":
        return None
    try:
        ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        ax.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        ax.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        if prompt:
            # Build a CFDictionary {kAXTrustedCheckOptionPrompt: kCFBooleanTrue} via CoreFoundation.
            cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            cf.CFDictionaryCreate.restype = ctypes.c_void_p
            cf.CFStringCreateWithCString.restype = ctypes.c_void_p
            key_str = cf.CFStringCreateWithCString(None, b"AXTrustedCheckOptionPrompt", 0x08000100)
            # kCFBooleanTrue is a fixed address in CFBoolean
            cf.CFBooleanGetValue.restype = ctypes.c_bool
            cftrue_sym = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
            keys = (ctypes.c_void_p * 1)(key_str)
            vals = (ctypes.c_void_p * 1)(cftrue_sym)
            opts = cf.CFDictionaryCreate(None, keys, vals, 1, None, None)
            result = bool(ax.AXIsProcessTrustedWithOptions(opts))
        else:
            result = bool(ax.AXIsProcessTrustedWithOptions(None))
        return result
    except (OSError, AttributeError, Exception):
        return None


class ComputerNode:
    def __init__(self, state_dir, bridge=None):
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        bridge_env = {key: value for key, value in os.environ.items() if key != "MORNING_DEVICE_TOKEN" and not any(part in key for part in ("API_KEY", "AUTH_TOKEN", "ACCESS_TOKEN", "SECRET_ACCESS_KEY"))}
        self.bridge = bridge or McpBridge(default_bridge_command(), env=bridge_env)
        self.journal = Journal(str(self.root))
        self.journal.mark_interrupted()
        self.lock = threading.RLock()
        self.policy_lock = threading.RLock()
        # Pre-warm the MCP bridge in the background so the first inventory call
        # doesn't pay the cold-start cost. Errors here are non-fatal; they'll
        # surface naturally on the first real call.
        threading.Thread(target=self._warmup, name="mcp-warmup", daemon=True).start()
        self.observations = {}
        self.policy = {"observe_apps": [], "operate_apps": [], "screens": [], "paused": False, "revision": 0,
                       "observe_all": True, "operate_all": False, "exclude_apps": list(DEFAULT_EXCLUDED)}
        if (self.root / "permissions.json").exists():
            self.policy.update(json.loads((self.root / "permissions.json").read_text()))

    def _warmup(self):
        """Start the MCP bridge process eagerly so the first inventory call is instant."""
        try:
            self.bridge.ensure()
        except Exception:
            pass  # Will surface naturally on first real call

    def configure(self, body):
        with self.policy_lock:
            if set(body) - {"observe_apps", "operate_apps", "screens", "paused", "observe_all", "operate_all", "exclude_apps"}:
                raise ComputerError("invalid_permissions", "设备权限包含未知字段。")
            value = {**self.policy, **body}
            for key in ("observe_apps", "operate_apps", "exclude_apps"):
                if not isinstance(value[key], list) or len(value[key]) > 100 or any(not isinstance(a, str) or not BUNDLE_ID.fullmatch(a) for a in value[key]):
                    raise ComputerError("invalid_apps", "请使用应用的 bundle ID 设置权限。")
            if not set(value["operate_apps"]) <= set(value["observe_apps"]):
                raise ComputerError("observation_required", "操作应用前需要允许观察同一应用。")
            if not isinstance(value["screens"], list) or any(type(i) is not int or i < 1 or i > 32 for i in value["screens"]):
                raise ComputerError("invalid_screens", "请指定有效的显示器编号。")
            if type(value["paused"]) is not bool:
                raise ComputerError("invalid_pause", "暂停状态需要是开关。")
            if type(value["observe_all"]) is not bool or type(value["operate_all"]) is not bool:
                raise ComputerError("invalid_permissions", "完全访问需要是开关。")
            if value["operate_all"] and not value["observe_all"]:
                raise ComputerError("observation_required", "操作全部应用前需要允许观察全部应用。")
            value["revision"] += 1
            write_private(self.root / "permissions.json", value)
            self.policy = value
            return value

    def health(self):
        return {"protocol": PROTOCOL, "name": "本机", "platform": os.uname().sysname,
                "permissions": dict(self.policy), "actions": list(ACTION_FIELDS),
                "bridge": self.bridge.status(), "model_required": False,
                "screen_recording_permission": screen_permission(),
                "accessibility_permission": accessibility_permission()}

    def open_system_settings(self, permission):
        """Open System Settings to the exact privacy pane — no misleading dialog,
        no python3 impersonation. The user grants access in the native macOS UI."""
        if sys.platform != "darwin":
            raise ComputerError("unsupported_platform", "系统设置只能在 macOS 上打开。")
        urls = {
            "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            "screen_recording": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
        }
        url = urls.get(permission)
        if not url:
            raise ComputerError("invalid_permission", "请选择 accessibility 或 screen_recording。")
        subprocess.Popen(["open", url])
        return {"opened": True, "permission": permission,
                "accessibility_permission": accessibility_permission(),
                "screen_recording_permission": screen_permission()}

    def inventory(self):
        result = self.bridge.call("list_apps", {})
        if result["is_error"]:
            raise ComputerError("inventory_unavailable", "无法读取应用列表，请检查桌面桥接与系统权限。")
        displays, display_error = [], None
        try:
            from mss import mss
            with mss() as screen:
                displays = [{"id": i, **rect} for i, rect in enumerate(screen.monitors) if i]
        except Exception:
            display_error = "屏幕捕获尚未可用；请检查录屏权限和 mss 依赖。"
        if screen_permission() is False:
            display_error = "Computer 节点尚未获得 macOS 录屏权限；应用窗口观察由已授权的桌面桥接提供。"
        return {"applications": result["text"], "apps": parse_apps(result["text"]), "displays": displays, "display_error": display_error,
                "permissions": dict(self.policy)}

    def observable(self, app):
        policy = self.policy
        return app not in policy["exclude_apps"] and (policy["observe_all"] or app in policy["observe_apps"])

    def overview(self, body):
        """Running apps at a glance. Uses the accessibility tree only — no screenshot, no screen
        recording permission required. Observable apps get their window title and a shallow
        summary of top-level elements; non-observable apps are listed by name only so the grid
        stays honest without silently omitting anything."""
        limit = body.get("limit", 6)
        if type(limit) is not int or not 1 <= limit <= 12:
            raise ComputerError("invalid_limit", "窗口数量需要在 1 到 12 之间。")
        result = self.bridge.call("list_apps", {})
        if result["is_error"]:
            raise ComputerError("inventory_unavailable", "无法读取应用列表，请检查桌面桥接与系统权限。")
        running = [app for app in parse_apps(result["text"]) if app["running"]]
        running.sort(key=lambda app: not app["frontmost"])
        windows = []
        for app in running[:limit]:
            item = {**app, "observable": self.observable(app["id"]), "at": time.time()}
            if item["observable"]:
                try:
                    with self.lock:
                        tree, _image = self._read(app["id"])
                    item["window"] = tree.window
                    item["sensitive"] = tree.has_sensitive()
                    # Shallow element summary — enough to make the card meaningful without
                    # sending the whole tree. Top-level nodes only (depth == 1 after the root).
                    summary = []
                    for node in tree.nodes:
                        if node.depth > 2:
                            break
                        label = node.description or node.head
                        if label:
                            summary.append(label[:60])
                        if len(summary) >= 6:
                            break
                    item["summary"] = summary
                except ComputerError as exc:
                    item["error"] = str(exc)
            windows.append(item)
        return {"windows": windows, "at": time.time(), "permissions": dict(self.policy)}

    def _allowed(self, app, operation=False):
        if operation and self.policy["paused"]:
            raise ComputerError("taken_over", "电脑已暂停，由你接管。", 409)
        if app in self.policy["exclude_apps"]:
            raise ComputerError("permission_required", "这个应用被排除在电脑权限之外。", 403)
        if not self.policy["operate_all" if operation else "observe_all"] and app not in self.policy["operate_apps" if operation else "observe_apps"]:
            raise ComputerError("permission_required", "请先在 Devices 中授权这个应用。", 403)

    def _read(self, app):
        result = self.bridge.call("get_app_state", {"app": app, "disableDiff": True})
        if result["is_error"]:
            raise ComputerError("observation_failed", "没有读到应用窗口，请检查应用与辅助功能权限。")
        try:
            tree = parse(result["text"])
        except TreeError:
            raise ComputerError("observation_incomplete", "电脑桥接未返回完整窗口，无法核验操作目标。请检查执行节点的桥接版本。", 409) from None
        if tree.bundle_id != app:
            raise ComputerError("app_mismatch", "读取到的应用不符合授权范围。", 409)
        image = None
        # Do not transmit an unredacted screenshot of a credential form to Pi.
        if result["images"] and not tree.has_sensitive():
            mime, data = result["images"][0]
            if mime in ("image/png", "image/jpeg"):
                image = {"mimeType": mime, "data": data}
        return tree, image

    def observe(self, body):
        app = body.get("app")
        with self.lock:
            self._allowed(app)
            tree, image = self._read(app)
            reference = secrets.token_urlsafe(18)
            text = tree.render()[:60000]
            value = {"id": reference, "app": app, "window": tree.window, "text": text, "image": image,
                     "fingerprint": fingerprint(text), "at": time.time(), "permission_revision": self.policy["revision"], "actions": SCHEMAS,
                     "elements": [{"index": str(node.index), "label": "敏感输入" if node.sensitive else (node.description or node.head)[:180]} for node in tree.nodes]}
            self.observations[reference] = value
            self.observations = {key: val for key, val in self.observations.items() if time.time() - val["at"] < 300}
            return value

    def screen(self, body):
        display = body.get("display")
        with self.policy_lock:
            if not self.policy["observe_all"] and display not in self.policy["screens"]:
                raise ComputerError("screen_permission_required", "请先授权这个显示器的观察权限。", 403)
        if screen_permission() is False:
            raise ComputerError("system_screen_permission_required", "请在 macOS 隐私设置中为运行 Computer 节点的程序授予录屏权限。", 403)
        from mss import mss
        from PIL import Image
        with mss() as capture:
            if type(display) is not int or display < 1 or display >= len(capture.monitors):
                raise ComputerError("display_missing", "显示器已经断开。")
            rect = dict(capture.monitors[display])
            region = body.get("region")
            if region:
                if not isinstance(region, dict) or set(region) != {"left", "top", "width", "height"} or any(type(v) is not int for v in region.values()):
                    raise ComputerError("invalid_region", "屏幕区域需要整数边界。")
                if region["width"] < 1 or region["height"] < 1 or region["left"] < 0 or region["top"] < 0 or region["left"] + region["width"] > rect["width"] or region["top"] + region["height"] > rect["height"]:
                    raise ComputerError("invalid_region", "观察区域超出显示器范围。")
                rect = {**region, "left": rect["left"] + region["left"], "top": rect["top"] + region["top"]}
            shot = capture.grab(rect)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
        small = img.convert("L").resize((32, 32))
        signature = [v // 16 for v in small.getdata()]
        img.thumbnail((1280, 900))
        stream = io.BytesIO()
        img.save(stream, "JPEG", quality=72)
        return {"display": display, "rect": rect, "at": time.time(), "signature": signature,
                "image": {"mimeType": "image/jpeg", "data": base64.b64encode(stream.getvalue()).decode()}}

    def act(self, body):
        key, app, method, arguments = body.get("key"), body.get("app"), body.get("method"), body.get("arguments", {})
        if not isinstance(key, str) or not 8 <= len(key) <= 120:
            raise ComputerError("key_required", "操作需要唯一标识。")
        if method not in ACTION_FIELDS or not isinstance(arguments, dict) or set(arguments) - ACTION_FIELDS[method]:
            raise ComputerError("invalid_action", "无法识别的电脑操作参数。")
        try:
            validate_action(method, arguments)
        except Exception:
            raise ComputerError("invalid_action", "操作参数无效；请严格使用最近观察返回的动作说明。") from None
        request_digest = fingerprint({"app": app, "method": method, "arguments": arguments, "observation_id": body.get("observation_id")})
        with self.lock:
            previous = self.journal.get(key)
            if previous:
                if previous["request_digest"] != request_digest:
                    raise ComputerError("key_conflict", "同一操作标识不能绑定不同操作。", 409)
                return {"key": key, "status": previous["status"], "deduplicated": True, "result": previous.get("result")}
            self._allowed(app, operation=True)
            observation = self.observations.get(body.get("observation_id"))
            if not observation or observation["app"] != app or time.time() - observation["at"] > 120 or observation["permission_revision"] != self.policy["revision"]:
                raise ComputerError("stale_observation", "观察已过期或授权发生变化，请重新观察并确认。", 409)
            tree, fresh_image = self._read(app)
            if fingerprint(tree.render()[:60000]) != observation["fingerprint"]:
                raise ComputerError("screen_changed", "窗口已变化，这次操作没有执行。请重新观察。", 409)
            if tree.has_sensitive():
                raise ComputerError("human_credentials", "当前窗口包含凭据字段，请由你手动处理。", 403)
            for name, value in arguments.items():
                if isinstance(value, str) and len(value) > 10000:
                    raise ComputerError("input_too_long", "单次输入过长，请拆分操作。")
            coordinates = method == "drag" or method == "click" and index_missing(arguments)
            if coordinates:
                from PIL import Image, ImageChops
                original = observation.get("image")
                if not original or not fresh_image:
                    raise ComputerError("screenshot_required", "坐标操作需要一张可用的应用截图。")
                with Image.open(io.BytesIO(base64.b64decode(original["data"]))) as img, Image.open(io.BytesIO(base64.b64decode(fresh_image["data"]))) as fresh:
                    width, height = img.size
                    if img.size != fresh.size or ImageChops.difference(img.convert("RGB"), fresh.convert("RGB")).getbbox():
                        raise ComputerError("screen_changed", "应用画面已经变化，请重新观察后准备坐标操作。", 409)
                pairs = [(arguments.get("x"), arguments.get("y"))] if method == "click" else [(arguments.get("from_x"), arguments.get("from_y")), (arguments.get("to_x"), arguments.get("to_y"))]
                if any(type(x) not in (int, float) or type(y) not in (int, float) or not math.isfinite(x) or not math.isfinite(y) or not 0 <= x < width or not 0 <= y < height for x, y in pairs):
                    raise ComputerError("outside_window", "坐标必须位于刚观察到的应用截图内。")
            if method == "press_key" and normalise_key(arguments.get("key")) in SYSTEM_KEYS:
                raise ComputerError("system_shortcut", "系统级敏感快捷键需要人工操作。", 403)
            index = arguments.get("element_index")
            if index is not None:
                node = tree.by_index(int(index)) if str(index).isdigit() else None
                if not node or node.disabled or node.sensitive:
                    raise ComputerError("invalid_element", "目标元素不可操作。")
            # Permission revocation/takeover wins even if observation was in flight.
            with self.policy_lock:
                self._allowed(app, operation=True)
                self.journal.begin(key, None, app, request_digest=request_digest)
                self.journal.append(key, "mutating", {"method": method, "at": time.time()})
            try:
                sent = self.bridge.call(method, {"app": app, **arguments})
                if sent["is_error"]:
                    raise ComputerError("action_uncertain", "操作结果尚未确认。")
                after = self.observe({"app": app})
                result = {"status": "observed", "after": after, "message": "动作已发送并重新观察；请根据新状态核验目标。"}
                self.journal.update(key, status="finished", finished_at=time.time(), result={"observation_id": after["id"], "status": "observed"})
                return {"key": key, **result}
            except Exception:
                self.journal.update(key, status="unknown", finished_at=time.time(), result={"status": "unknown"})
                return {"key": key, "status": "unknown", "message": "结果未知，请重新观察。相同操作不会重发。"}

    def close(self):
        self.bridge.close()

    def activate(self, body):
        """Bring an app's window to the foreground using AppleScript."""
        app = body.get("app")
        if not app or not isinstance(app, str):
            raise ComputerError("invalid_app", "需要提供应用 bundle ID。")
        self._allowed(app)
        try:
            import subprocess as _sp
            script = f'tell application id "{app}" to activate'
            result = _sp.run(["osascript", "-e", script], capture_output=True, timeout=5)
            if result.returncode != 0:
                raise ComputerError("activate_failed", f"激活失败：{result.stderr.decode().strip()}")
        except ComputerError:
            raise
        except Exception as exc:
            raise ComputerError("activate_failed", f"激活应用时出错：{exc}") from exc
        return {"activated": app}


def index_missing(arguments):
    return arguments.get("element_index") is None


def make_node_handler(node, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            status = 200
            try:
                if not token or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                    raise ComputerError("unauthenticated", "设备认证失败。", 401)
                if self.headers.get("Origin"):
                    raise ComputerError("console_required", "设备只能通过受信服务访问。", 403)
                body = {}
                if self.command == "POST":
                    size = int(self.headers.get("Content-Length", 0))
                    if not 0 < size <= 128000:
                        raise ComputerError("invalid_body", "请求长度无效。")
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body, dict):
                        raise ComputerError("invalid_body", "请求需要是对象。")
                route = {("GET", "/health"): node.health, ("GET", "/inventory"): node.inventory, ("POST", "/overview"): lambda: node.overview(body),
                         ("POST", "/observe"): lambda: node.observe(body), ("POST", "/screen"): lambda: node.screen(body),
                         ("POST", "/act"): lambda: node.act(body), ("POST", "/permissions"): lambda: node.configure(body),
                         ("POST", "/activate"): lambda: node.activate(body),
                         ("POST", "/open_system_settings"): lambda: node.open_system_settings(body.get("permission", "accessibility"))}.get((self.command, self.path))
                if route is None:
                    raise ComputerError("not_found", "设备接口不存在。", 404)
                value = route()
            except ComputerError as exc:
                status, value = exc.status, {"error": {"code": exc.code, "message": str(exc)}}
            except Exception:
                status, value = 503, {"error": {"code": "node_unavailable", "message": "设备操作不可用，请检查运行节点和系统权限。"}}
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="Morning Computer · independent execution node")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args(argv)
    token = os.environ.get("MORNING_DEVICE_TOKEN", "")
    if not token:
        parser.error("MORNING_DEVICE_TOKEN is required; never pass tokens on the command line")
    os.umask(0o077)
    node = ComputerNode(args.state)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_node_handler(node, token))
    print(json.dumps({"protocol": PROTOCOL, "port": server.server_address[1]}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        node.close()
        server.server_close()


if __name__ == "__main__":
    main()
