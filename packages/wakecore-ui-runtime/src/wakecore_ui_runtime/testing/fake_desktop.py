"""Stand-ins for the desktop runtime's two peers: a Computer Use MCP bridge and a model.

FakeDesktopMcp (a stdio MCP server, run as `python -m wakecore_ui_runtime.testing.fake_desktop STATE.json`)
speaks the tool surface of tmustier/codex-computer-use-mcp (get_app_state, click, set_value,
type_text, press_key, scroll, select_text, perform_secondary_action, list_apps,
computer_use_status) over two apps:
  com.apple.calculator  a working calculator: digits, add, multiply, equals, all clear
  com.example.signin    a sign-in window with a secure password field (value "hunter2")
Its state lives in the JSON file, so it survives a bridge restart and tests can read what
reached the "app" (`calls`, `display`). `faults` in the same file:
  {"hang_on": "click"}   that tool never answers (the runtime must time out, effect unknown)
  {"error_on": "click"}  that tool answers isError
  {"wrong_bundle": true} every get_app_state answers for com.apple.finder
  {"diff": true}         get_app_state without disableDiff answers with a diff, not a tree

FakeDesktopModel (HTTP, POST /v1/responses) answers `desktop_actions` function calls from a
policy and records every request body (tests assert that no hidden value ever reached it):
  calc          AC 1 2 x 3 =, then DONE with the display
  password      set_value into the password field
  type_login    type_text while a password field is on screen
  system_key    cmd+tab (refused), then DONE
  bad_index     click element 9999 (refused), then DONE
  disabled      click the disabled zoom button (refused), then DONE
  one           click "1" once, then DONE
  forever       click "1" every turn (runs into max_steps)
  other_tool    call a tool that is not desktop_actions
  needs_login   reply NEEDS_LOGIN
"""
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNoaGgAAAMEAYFL09IQAAAAAElFTkSuQmCC"
CALC, SIGNIN = "com.apple.calculator", "com.example.signin"
BUTTONS = [("1", "One"), ("2", "Two"), ("3", "Three"), ("4", "Four"), ("5", "Five"), ("6", "Six"),
           ("7", "Seven"), ("8", "Eight"), ("9", "Nine"), ("0", "Zero"), ("multiply", "Multiply"),
           ("add", "Add"), ("equals", "Equals"), ("all clear", "AllClear")]
TOOLS = ("get_app_state", "click", "set_value", "type_text", "press_key", "scroll", "select_text",
         "perform_secondary_action", "list_apps", "computer_use_status")
FRESH = {"display": "0", "entry": "", "acc": None, "op": None, "user": "", "password": "hunter2", "calls": [],
         "faults": {}}


# ---------------------------------------------------------------------- the bridge

def _calc_tree(s: dict[str, Any]) -> str:
    lines = ["0 standard window Calculator, ID: main, Secondary Actions: Raise",
             "\t1 split group main",
             "\t\t2 scroll area Description: display, ID: StandardInputView, Secondary Actions: Copy",
             f"\t\t\t3 text {s['display']}"]
    for i, (desc, ident) in enumerate(BUTTONS):
        lines.append(f"\t\t{4 + i} button Description: {desc}, ID: {ident}")
    lines.append(f"\t{4 + len(BUTTONS)} zoom button (disabled)")
    return ("App=/System/Applications/Calculator.app/ (bundleID {b}, pid 4242)\n"
            'Window: "Calculator", App: Calculator.\n').format(b=CALC) + "\n".join(lines)


def _signin_tree(s: dict[str, Any]) -> str:
    return (f"App=/Applications/SignIn.app/ (bundleID {SIGNIN}, pid 4343)\n"
            'Window: "Sign in", App: SignIn.\n'
            "0 standard window Sign in\n"
            f"\t1 text field Description: user name, Value: {s['user']}\n"
            f"\t2 secure text field Description: password, Value: {s['password']}\n"
            "\t3 button Sign In")


def _press(s: dict[str, Any], ident: str) -> None:
    digits = {i: d for d, i in BUTTONS[:10]}
    if ident in digits:
        s["entry"] = (s["entry"] + digits[ident]).lstrip("0") or "0"
        s["display"] = s["entry"]
    elif ident in ("Multiply", "Add"):
        s["acc"], s["op"], s["entry"] = float(s["display"]), ident, ""
    elif ident == "Equals" and s["op"]:
        x = float(s["display"])
        r = s["acc"] * x if s["op"] == "Multiply" else s["acc"] + x
        s["display"], s["acc"], s["op"], s["entry"] = (str(int(r)) if r == int(r) else str(r)), None, None, ""
    elif ident == "AllClear":
        s.update(display="0", entry="", acc=None, op=None)


class FakeDesktopMcp:
    def __init__(self, state_path: str) -> None:
        self.path = state_path
        self.seen: set[str] = set()     # apps whose full tree this process already sent (diff mode)

    def load(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return {**FRESH, **json.load(f)}
        except FileNotFoundError:
            return json.loads(json.dumps(FRESH))

    def save(self, s: dict[str, Any]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f)
        os.replace(tmp, self.path)

    @staticmethod
    def _text(text: str, *, error: bool = False, image: bool = False) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if image:
            content.append({"type": "image", "mimeType": "image/png", "data": PIXEL})
        return {"content": content, "isError": error}

    def tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        s = self.load()
        faults = s.get("faults") or {}
        if name == "computer_use_status":
            return {"content": [{"type": "text", "text": "ok"}], "isError": False,
                    "structuredContent": {"brokerVerified": True, "permissionMode": "fake", "clientBuild": "1",
                                          "brokerPath": "/Users/someone/secret/path"}}
        if name == "list_apps":
            return self._text(f"Calculator — {CALC}\nSignIn — {SIGNIN}")
        app = args.get("app")
        if app not in (CALC, SIGNIN):
            return self._text(f"app {app!r} is not running", error=True)
        if name == "get_app_state":
            if faults.get("diff") and not args.get("disableDiff") and app in self.seen:
                return self._text("<app_state_diff>\n~ 3 text 12\n</app_state_diff>")
            self.seen.add(app)
            tree = _calc_tree(s) if app == CALC else _signin_tree(s)
            if faults.get("wrong_bundle"):
                tree = tree.replace(f"bundleID {app}", "bundleID com.apple.finder")
            return self._text(f"<app_state>\n{tree}\n</app_state>", image=True)
        # an action: it reaches the app
        s["calls"].append({"tool": name, "args": args, "at": time.time()})
        self.save(s)
        if faults.get("hang_on") == name:
            time.sleep(3600)
        if faults.get("error_on") == name:
            return self._text(f"{name} failed: the element went away", error=True)
        if name == "click" and app == CALC:
            idx = int(args.get("element_index", -1))
            if 4 <= idx < 4 + len(BUTTONS):
                _press(s, BUTTONS[idx - 4][1])
        elif name == "set_value" and app == SIGNIN:
            s["user" if args.get("element_index") == "1" else "password"] = args.get("value", "")
        elif name == "type_text" and app == CALC:
            for ch in args.get("text", ""):
                ident = {d: i for d, i in BUTTONS[:10]}.get(ch) or {"*": "Multiply", "+": "Add", "=": "Equals"}.get(ch)
                if ident:
                    _press(s, ident)
        self.save(s)
        return self._text(f"{name} done")

    def serve(self, stdin: Any = None, stdout: Any = None) -> None:
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if "id" not in msg:
                continue                                       # a notification
            method, params = msg.get("method"), msg.get("params") or {}
            if method == "initialize":
                result: Any = {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                               "serverInfo": {"name": "fake-computer-use", "version": "0.0.1"}}
            elif method == "tools/list":
                result = {"tools": [{"name": t, "inputSchema": {"type": "object"}} for t in TOOLS]}
            elif method == "tools/call":
                if params.get("name") not in TOOLS:
                    stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                             "error": {"code": -32602, "message": "unknown tool"}}) + "\n")
                    stdout.flush()
                    continue
                result = self.tool(params["name"], params.get("arguments") or {})
            else:
                stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                         "error": {"code": -32601, "message": "no such method"}}) + "\n")
                stdout.flush()
                continue
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}, ensure_ascii=False) + "\n")
            stdout.flush()


def write_state(path: str, **changes: Any) -> dict[str, Any]:
    """Reset (or change) the fake app state a FakeDesktopMcp process reads."""
    s = json.loads(json.dumps(FRESH))
    s.update(changes)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(s, f)
    return s


def read_state_file(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def mcp_command(state_path: str) -> list[str]:
    return [sys.executable, "-m", "wakecore_ui_runtime.testing.fake_desktop", state_path]


# ---------------------------------------------------------------------- the model

def _latest_tree(body: dict[str, Any]) -> str:
    tree = ""
    for item in body.get("input") or []:
        for part in item.get("content") or [] if isinstance(item.get("content"), list) else []:
            if part.get("type") == "input_text" and str(part.get("text", "")).startswith("Accessibility tree of "):
                tree = part["text"]
    return tree


def element(tree: str, *, ident: Optional[str] = None, desc: Optional[str] = None,
            head: Optional[str] = None) -> Optional[int]:
    for line in tree.split("\n"):
        m = re.match(r"^\t*(\d+) (.*)$", line)
        if not m:
            continue
        rest = m.group(2)
        if ident and re.search(rf"\bID: {re.escape(ident)}\b", rest):
            return int(m.group(1))
        if desc and f"Description: {desc}" in rest:
            return int(m.group(1))
        if head and rest.startswith(head):
            return int(m.group(1))
    return None


class FakeDesktopModel:
    def __init__(self, policy: str = "calc", api_key: str = "sk-test-fake-desktop") -> None:
        self.policy, self.api_key = policy, api_key
        self.requests: list[str] = []
        self.lock = threading.Lock()
        self._turns: dict[str, int] = {}
        self._n = 0
        self.httpd: Optional[ThreadingHTTPServer] = None

    def start(self) -> "FakeDesktopModel":
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                status, body = fake.handle(self.headers.get("Authorization", ""), raw)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    @property
    def base_url(self) -> str:
        assert self.httpd is not None
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    def handle(self, auth: str, raw: str) -> tuple[int, dict[str, Any]]:
        with self.lock:
            self.requests.append(raw)
            if auth != f"Bearer {self.api_key}":
                return 401, {"error": {"message": "bad key"}}
            body = json.loads(raw)
            tools = [t.get("name") for t in body.get("tools") or []]
            if tools != ["desktop_actions"]:
                return 400, {"error": {"message": "expected exactly the desktop_actions tool"}}
            prev = body.get("previous_response_id")
            turn = self._turns.get(prev, -1) + 1 if prev else 0
            self._n += 1
            rid = f"resp_{self._n}"
            self._turns[rid] = turn
            out = self.decide(turn, _latest_tree(body))
            return 200, {"id": rid, "output": out, "usage": {"input_tokens": 100, "output_tokens": 10}}

    def _call(self, *actions: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"type": "function_call", "name": "desktop_actions", "call_id": f"call_{self._n}",
                 "arguments": json.dumps({"actions": list(actions)})}]

    @staticmethod
    def _say(text: str) -> list[dict[str, Any]]:
        return [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]

    def decide(self, turn: int, tree: str) -> list[dict[str, Any]]:
        p = self.policy
        click = lambda **kw: {"type": "click", "element": element(tree, **kw)}   # noqa: E731
        if p == "calc":
            if turn == 0:
                return self._call(click(ident="AllClear"), click(ident="One"), click(ident="Two"),
                                  click(ident="Multiply"), click(ident="Three"), click(ident="Equals"))
            m = re.search(r"\n\t*3 text (\S+)", tree)
            return self._say(f"DONE the display shows {m.group(1) if m else '?'}")
        if p == "password":
            return self._call({"type": "set_value", "element": element(tree, desc="password"), "value": "x"}) \
                if turn == 0 else self._say("DONE")
        if p == "type_login":
            return self._call({"type": "type_text", "text": "hello"}) if turn == 0 else self._say("DONE")
        if p == "system_key":
            return self._call({"type": "press_key", "key": "cmd+tab"}) if turn == 0 else self._say("DONE")
        if p == "bad_index":
            return self._call({"type": "click", "element": 9999}) if turn == 0 else self._say("DONE")
        if p == "disabled":
            return self._call(click(head="zoom button")) if turn == 0 else self._say("DONE")
        if p == "one":
            return self._call(click(ident="One")) if turn == 0 else self._say("DONE pressed 1")
        if p == "forever":
            return self._call(click(ident="One"))
        if p == "other_tool":
            return [{"type": "function_call", "name": "open_app", "call_id": "call_x", "arguments": "{}"}]
        if p == "needs_login":
            return self._say("NEEDS_LOGIN the app asks for a password")
        return self._say("NEEDS_HUMAN unknown policy")


if __name__ == "__main__":
    FakeDesktopMcp(sys.argv[1]).serve()
