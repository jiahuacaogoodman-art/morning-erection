"""An HTTP stand-in for OpenAI's Responses API computer-use loop (no key, no network).

It speaks the wire protocol the UI Runtime's real client uses — POST /v1/responses with a
computer tool, `computer_call` outputs, `computer_call_output` screenshot replies chained
by `previous_response_id` — and *checks* it: wrong call ids, missing/invalid PNG
screenshots, a broken chain or a missing key are protocol errors, answered with 400/401
and recorded in `violations`.

Three protocol variants:
  ga       tools=[{"type": "computer"}]; computer_call.actions = [..batched actions..]
  preview  tools=[{"type": "computer_use_preview", display_*, environment}]; truncation=auto;
           computer_call.action = {..one action..}; may carry pending_safety_checks
  function tools=[{"type": "function", "name": "computer_actions", ...}]; a function_call whose
           arguments are {"actions": [...]}; answered by function_call_output + a user input_image

stateless=True behaves like a relay without server-side storage: previous_response_id is refused
(`refusal`, by default "previous_response_id is not available for this user") and a turn is accepted only if it resends
the conversation: the task first, the call being answered, no item ids.

It cannot see pixels, so "vision" is simulated: policies read the portal's page state and
fixed geometry (what a vision model would infer from the screenshot). Policies:
  oracle        does the task: 选课 -> 确认选课 -> done
  gullible      obeys the injected announcement: types a password into it, then follows its link
  offsite       clicks the phishing link, then claims success
  stall         keeps asking to wait
  error500      the API is down
  safety_check  (preview) returns pending_safety_checks on the first call
  double_submit submits, goes back, and submits the same form again

Every request body is kept in `requests` so tests can prove that no password, cookie or
session token was ever sent to the model.
"""
import base64
import json
import re
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .jw_portal import JwPortal

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class FakeOpenAI:
    def __init__(self, portal: Optional[JwPortal] = None, *, policy: str = "oracle", variant: str = "ga",
                 api_key: str = "sk-test-fake-computer-use", user: str = "alice", stateless: bool = False,
                 drop_tools: bool = False, blind: bool = False,
                 refusal: str = "previous_response_id is not available for this user") -> None:
        self.portal, self.policy, self.variant, self.api_key, self.user = portal, policy, variant, api_key, user
        self.stateless = stateless   # like a relay that forwards only single requests: chaining fails
        self.refusal = refusal       # what it says when it refuses chaining
        self.drop_tools = drop_tools  # like a relay that strips `tools`: the model never sees the computer
        self.blind = blind            # like a relay that strips images
        self._colour: Optional[str] = None   # colour of the last screenshot received
        self.lock = threading.Lock()
        self.requests: list[str] = []
        self.violations: list[str] = []
        self.screenshots = 0
        self._chains: dict[str, dict[str, Any]] = {}
        self._by_call: dict[str, dict[str, Any]] = {}    # pending call_id -> chain (resent history)
        self.history_turns = 0                            # turns that arrived as resent history
        self._n = 0
        self.httpd: Optional[ThreadingHTTPServer] = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "FakeOpenAI":
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode()
                status, body = fake.handle(self.path, self.headers.get("Authorization", ""), raw)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def reset(self, *, policy: Optional[str] = None) -> None:
        with self.lock:
            self.requests.clear()
            self.violations.clear()
            self.screenshots = 0
            if policy:
                self.policy = policy

    # ------------------------------------------------------------------ protocol
    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}_{self._n:04d}"

    def _bad(self, status: int, msg: str) -> tuple[int, dict[str, Any]]:
        self.violations.append(msg)
        return status, {"error": {"message": msg, "type": "invalid_request_error"}}

    def handle(self, path: str, auth: str, raw: str) -> tuple[int, dict[str, Any]]:
        with self.lock:
            self.requests.append(raw)
            if path != "/v1/responses":
                return self._bad(404, f"unknown path {path}")
            if auth != f"Bearer {self.api_key}":
                return self._bad(401, "invalid api key")
            if self.policy == "error500":
                return 500, {"error": {"message": "upstream overloaded", "type": "server_error"}}
            try:
                req = json.loads(raw)
            except ValueError:
                return self._bad(400, "body is not json")
            err = "model is required" if self.drop_tools and not req.get("model") else \
                None if self.drop_tools else self._check_tools(req)
            if err:
                return self._bad(400, err)
            prev = req.get("previous_response_id")
            items = [i for i in req.get("input") or [] if isinstance(i, dict)] \
                if isinstance(req.get("input"), list) else []
            outs = [i for i in items if i.get("type") in ("computer_call_output", "function_call_output")]
            if prev is not None:
                if self.stateless:
                    return self._bad(400, self.refusal)
                chain = self._chains.get(prev)
                if chain is None:
                    return self._bad(400, "previous_response_id is unknown")
                err = self._check_reply(items, chain)
                if err:
                    return self._bad(400, err)
            elif not outs:
                goal = self._first_turn(req)
                if goal is None:
                    return self._bad(400, "first turn needs a user message with input_text")
                chain = {"goal": goal, "step": 0, "pending": None, "screens": 0}
            else:                        # the client resends the whole conversation
                if any("id" in i for i in items):
                    return self._bad(400, "input items carry ids, but nothing was stored server-side")
                chain = self._by_call.get(str(outs[-1].get("call_id")))
                if chain is None:
                    return self._bad(400, "the answered call_id is unknown")
                if self._goal_of(items[:1]) != chain["goal"]:
                    return self._bad(400, "resent history does not start with the task")
                at = [n for n, i in enumerate(items) if i.get("type") in ("computer_call", "function_call")
                      and i.get("call_id") == chain["pending"]]
                if not at:
                    return self._bad(400, "resent history lacks the call being answered")
                err = self._check_reply(items[at[-1] + 1:], chain)
                if err:
                    return self._bad(400, err)
                self.history_turns += 1
            rid = self._id("resp")
            item = self._decide(chain)
            pending = item.get("call_id") if item["type"] in ("computer_call", "function_call") else None
            chain = {**chain, "step": chain["step"] + 1, "pending": pending}
            self._chains[rid] = chain
            if pending:
                self._by_call[pending] = chain
            return 200, {"id": rid, "object": "response", "status": "completed", "model": req.get("model"),
                         "output": [item], "usage": {"input_tokens": 1000, "output_tokens": 20, "total_tokens": 1020}}

    def _check_tools(self, req: dict[str, Any]) -> Optional[str]:
        if not isinstance(req.get("model"), str) or not req["model"]:
            return "model is required"
        tools = req.get("tools") or []
        kinds = [t.get("type") for t in tools if isinstance(t, dict)]
        if self.variant == "ga":
            return None if kinds == ["computer"] else f"expected tools [computer], got {kinds}"
        if self.variant == "function":
            if kinds == ["computer"]:
                return "Unsupported tool type: computer"
            ok = kinds == ["function"] and tools[0].get("name") == "computer_actions" \
                and isinstance(tools[0].get("parameters"), dict)
            return None if ok else f"expected one function tool computer_actions, got {kinds}"
        if kinds != ["computer_use_preview"]:
            return f"expected tools [computer_use_preview], got {kinds}"
        t = tools[0]
        if not (isinstance(t.get("display_width"), int) and isinstance(t.get("display_height"), int)
                and t.get("environment") == "browser"):
            return "computer_use_preview needs display_width/display_height/environment=browser"
        if req.get("truncation") != "auto":
            return "computer_use_preview requires truncation=auto"
        return None

    def _first_turn(self, req: dict[str, Any]) -> Optional[str]:
        items = req.get("input")
        if not isinstance(items, list):
            return None
        texts = []
        for it in items:
            if isinstance(it, dict) and it.get("role") == "user":
                for part in it.get("content") or []:
                    if part.get("type") == "input_text":
                        texts.append(part.get("text", ""))
                    elif part.get("type") == "input_image":
                        if not self._png(part.get("image_url")):
                            return None
                        self.screenshots += 1
        return "\n".join(texts) if texts else None

    @staticmethod
    def _goal_of(items: list[dict[str, Any]]) -> Optional[str]:
        texts = [p.get("text", "") for i in items if i.get("role") == "user"
                 for p in i.get("content") or [] if isinstance(p, dict) and p.get("type") == "input_text"]
        return "\n".join(texts) if texts else None

    def _check_reply(self, items: list[dict[str, Any]], chain: dict[str, Any]) -> Optional[str]:
        if chain["pending"] is None:
            return "no computer_call is pending on previous_response_id"
        if self.variant == "function":
            outs = [i for i in items if i.get("type") == "function_call_output"]
            if len(outs) != 1 or outs[0].get("call_id") != chain["pending"]:
                return "function_call_output.call_id does not match the pending call"
            images = [p for i in items if i.get("role") == "user" for p in i.get("content") or []
                      if isinstance(p, dict) and p.get("type") == "input_image"]
            if len(images) != 1 or not self._png(images[0].get("image_url")):
                return "a function_call_output needs one user input_image with a PNG data URL"
            if images[0].get("detail") != "original":
                return "screenshots should use detail=original (click accuracy)"
            self.screenshots += 1
            return None
        outs = [i for i in items if i.get("type") == "computer_call_output"]
        if len(outs) != 1 or outs[0].get("call_id") != chain["pending"]:
            return "computer_call_output.call_id does not match the pending call"
        out = outs[0].get("output") or {}
        if out.get("type") != "computer_screenshot" or not self._png(out.get("image_url")):
            return "computer_call_output needs a computer_screenshot with a PNG data URL"
        if self.variant == "ga" and out.get("detail") != "original":
            return "ga screenshots should use detail=original (click accuracy)"
        if outs[0].get("acknowledged_safety_checks"):
            self.violations.append("safety checks were acknowledged")  # the runtime must never do this
        self.screenshots += 1
        return None

    def _png(self, url: Any) -> bool:
        prefix = "data:image/png;base64,"
        if not isinstance(url, str) or not url.startswith(prefix):
            return False
        try:
            png = base64.b64decode(url[len(prefix):], validate=True)
        except ValueError:
            return False
        self._colour = None if self.blind else colour_of(png)
        return png[:8] == PNG_MAGIC

    # ------------------------------------------------------------------ "vision"
    def _call(self, *actions: dict[str, Any], safety: Optional[list] = None) -> dict[str, Any]:
        cid = self._id("call")
        if self.variant == "function":
            return {"type": "function_call", "id": self._id("fc"), "call_id": cid, "name": "computer_actions",
                    "arguments": json.dumps({"actions": list(actions)}), "status": "completed"}
        item: dict[str, Any] = {"type": "computer_call", "id": self._id("cu"), "call_id": cid,
                                "pending_safety_checks": safety or [], "status": "completed"}
        if self.variant == "ga":
            item["actions"] = list(actions)
        else:
            item["action"] = actions[0]
        return item

    def _say(self, text: str) -> dict[str, Any]:
        return {"type": "message", "id": self._id("msg"), "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text}]}

    @staticmethod
    def _click(p: dict[str, int]) -> dict[str, Any]:
        return {"type": "click", "button": "left", "x": p["x"], "y": p["y"]}

    def _decide(self, chain: dict[str, Any]) -> dict[str, Any]:
        if self.drop_tools:
            return self._say("NEEDS_HUMAN: no browser tool is available in this session.")
        if self.portal is None:      # no site behind it: only the connectivity check talks to this fake
            if chain["step"] == 0:
                return self._call(self._click({"x": 128, "y": 80}))
            return self._say(self._colour.capitalize() if self._colour else "I cannot see any screenshot.")
        snap, geo = self.portal.snapshot(), self.portal.geometry()
        step, page = chain["step"], snap["last_page"].get(self.user, "/courses")
        if step >= 12:
            return self._say("我已尝试多次，停止操作。")
        if self.policy == "stall":
            return self._call({"type": "wait"})
        if self.policy == "safety_check" and step == 0:
            return self._call({"type": "screenshot"}, safety=[{"id": "cu_sc_1", "code": "malicious_instructions",
                                                              "message": "页面包含可疑指令"}])
        if self.policy == "gullible":
            script = [self._click(geo["notice_password"]), {"type": "type", "text": "hunter2"},
                      {"type": "keypress", "keys": ["ENTER"]}, self._click(geo["notice_link"])]
            return self._call(script[step]) if step < len(script) else self._say("已完成身份复核。")
        if self.policy == "offsite":
            return self._call(self._click(geo["notice_link"])) if step == 0 else self._say("DONE")
        target = next((c for c in sorted(snap["courses"], key=len, reverse=True)
                       if re.search(rf"\b{re.escape(c)}\b", chain["goal"])), None)
        if target is None:
            return self._say("我无法确定要选哪门课。")
        if f"{self.user}:{target}" in snap["enrollments"] and page != "/enroll/confirm" \
                and self.policy != "double_submit":
            return self._say("DONE：该课程已在我的课表中。")
        if page in ("/", "/courses"):
            if target not in geo["enroll_buttons"] or snap["courses"][target]["seats"] <= 0:
                return self._say("该课程目前没有余量，无法选课。")
            return self._call(self._click(geo["enroll_buttons"][target]))
        if self.policy == "double_submit":
            # thinks the first submit did not register: goes back and submits the same form again
            n = chain.setdefault("confirms", 0)
            if n >= 2:
                return self._say("DONE：已提交选课。")
            if page == "/enroll":
                chain["confirms"] = n + 1
                return self._call(self._click(geo["confirm_button"]))
            if page == "/enroll/confirm":
                return self._call({"type": "click", "button": "back", "x": 10, "y": 10})
        if page == "/enroll":
            return self._call(self._click(geo["confirm_button"]))
        if page == "/enroll/confirm":
            return self._say("DONE：已提交选课。")
        return self._say("页面不在预期流程中，停止。")


def colour_of(png: bytes) -> Optional[str]:
    """The simulator's "vision": the name of the first pixel's colour, for 8-bit RGB PNGs only."""
    try:
        pos, idat = 8, b""
        while pos < len(png):
            n = struct.unpack(">I", png[pos:pos + 4])[0]
            kind, data = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + n]
            if kind == b"IHDR" and data[8:10] != b"\x08\x02":
                return None
            idat += data if kind == b"IDAT" else b""
            pos += 12 + n
        raw = zlib.decompress(idat)
    except (struct.error, zlib.error):
        return None
    if len(raw) < 4 or raw[0] != 0:
        return None
    rgb = raw[1:4]
    names = {"red": (230, 30, 30), "white": (255, 255, 255), "black": (0, 0, 0),
             "green": (30, 200, 30), "blue": (30, 30, 230)}
    return min(names, key=lambda k: sum((a - b) ** 2 for a, b in zip(rgb, names[k])))
