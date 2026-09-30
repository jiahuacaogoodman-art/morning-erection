"""Model connectivity check: `wakecore-ui-runtime --check-model [--openai-base-url URL] ...`.

Two short Responses calls with the configured computer tool, no browser involved:
  1. start   a white PNG and the goal "click once with the computer tool, then name the colour
             of the screenshot you get back". Only the tool can click, so the answer MUST be a
             computer_call (variant function: a computer_actions function_call): a relay that accepts the request but drops `tools` makes the model
             answer in text ("no browser tool"), and the loop could never act through it.
  2. chain   a computer_call_output (variant function: function_call_output + input_image) with a
             red PNG on previous_response_id, or resent history if the endpoint refuses chaining. The answer SHOULD
             say "red": a model that cannot did not get the screenshot after its action.
Stateless relays fail step 2, relays without the Responses API fail step 1. The key is never
printed; error bodies pass through `redact()`. The two solid PNGs are the only images sent.
"""
import re
import struct
import zlib
from typing import Any

from .openai import ModelError, ResponsesClient, computer_calls, message_text, redact

GOAL = ("Connectivity check from WakeCore; no real page is involved and nothing can break. Use the "
        "{tool} tool to click once in the middle of the screen (x=128, y=80), and do nothing else. "
        "You will then receive a new screenshot: reply with one English word, the colour that fills "
        "that new screenshot.")
WHITE = (255, 255, 255)
RED = (230, 30, 30)
SAID_RED = re.compile(r"\bred\b|红", re.IGNORECASE)
MAX_CHAIN = 3     # a model may ask for another screenshot before it answers

HINTS = {
    "model_not_configured": "no key: pass --openai-key-file FILE (or set OPENAI_API_KEY)",
    "http_401": "the endpoint rejected the key",
    "http_403": "the key is not allowed to use this model or endpoint",
    "http_404": "nothing at {base}/responses: the base URL usually ends in /v1, and the endpoint must "
                "implement the Responses API (Chat Completions alone is not enough)",
    "http_429": "rate limited or out of quota",
    "model_unreachable": "no connection (DNS, TLS, proxy or firewall)",
    "model_bad_response": "the answer is not a Responses API response (needs `id` and `output[]`)",
    "computer_tool_unused": "the endpoint answered, but the model did not use the computer tool{said}. "
                            "Relays that drop `tools` behave like this; the browser can never be operated "
                            "through them. Use an endpoint that forwards the `{tool}` tool unchanged",
    "image_not_seen": "the model did not name the colour of the screenshot{said}; the endpoint may drop "
                      "image input, and the model would operate the page blind",
}
CHAT_ONLY = re.compile(r"chat[/_ .-]?completions", re.IGNORECASE)
NO_MODEL = re.compile(r"model_not_found|model\b[^.]{0,120}\b(not (supported|found|available)|does not exist)",
                      re.IGNORECASE)
HINT_400 = {
    "start": "the endpoint refused the request: check the model name and that it supports the "
             "`{tool}` tool with image input",
    "chain": "the endpoint refused previous_response_id chaining (a stateless relay?); the Computer "
             "Use loop needs it",
}


def solid_png(rgb: tuple[int, int, int] = RED, w: int = 256, h: int = 160) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _summary(resp: dict[str, Any]) -> dict[str, Any]:
    kinds = [o.get("type") for o in resp["output"] if isinstance(o, dict)]
    usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
    return {"ok": True, "output_types": kinds, "total_tokens": usage.get("total_tokens")}


def _hint(code: str, step: str, client: ResponsesClient, detail: str = "") -> str:
    if CHAT_ONLY.search(detail):
        return ("the endpoint says it offers only Chat Completions here; Computer Use needs the Responses "
                "API with the computer tool and image input, so this endpoint (or its current upstream) "
                "cannot drive the browser. Retrying will not help")
    if NO_MODEL.search(detail):
        return (f"the endpoint does not offer the model `{client.model}`; pass --model with one it has "
                "that supports the computer tool")
    if code == "http_400":
        return HINT_400[step].format(tool=client.tool_name)
    if code.startswith("http_5"):
        return "the endpoint (or the model behind it) failed; try again later"
    return HINTS.get(code, "").format(base=client.base_url)


def check_model(client: ResponsesClient) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "endpoint_host": client.endpoint_host, "model_egress": client.model_egress,
                           "model_ref": client.model_ref, "variant": client.variant, "steps": {}}
    png = solid_png()
    step = "start"
    try:
        resp = client.start(GOAL.format(tool=client.tool_name), solid_png(WHITE))
        calls = computer_calls(resp)
        if not calls or not calls[0].get("call_id"):
            return _fail(out, "start", "computer_tool_unused", client, message_text(resp), resp)
        out["steps"]["start"] = _summary(resp)
        step = "chain"
        for _ in range(MAX_CHAIN):
            resp = client.reply(resp["id"], calls[0]["call_id"], png)
            calls = computer_calls(resp)
            if not calls or not calls[0].get("call_id"):
                break
        said = message_text(resp)
        if calls or not said:
            out["steps"]["chain"] = {**_summary(resp), "image": "unconfirmed"}
        elif SAID_RED.search(said):
            out["steps"]["chain"] = {**_summary(resp), "image": "seen"}
        else:
            return _fail(out, "chain", "image_not_seen", client, said, resp)
    except ModelError as e:
        out["steps"][step] = {"ok": False, "error_code": e.code, "detail": e.detail[:300]}
        out.update(error_code=e.code, failed_step=step, hint=_hint(e.code, step, client, e.detail))
        return out
    out["ok"] = True
    out["history"] = client.history_in_use
    return out


def _fail(out: dict[str, Any], step: str, code: str, client: ResponsesClient, said: str,
          resp: dict[str, Any]) -> dict[str, Any]:
    tool = client.tool_name
    quoted = f' (it said: "{redact(said)[:160]}")' if said else ""
    out["steps"][step] = {**_summary(resp), "ok": False, "error_code": code}
    out.update(error_code=code, failed_step=step, hint=HINTS[code].format(said=quoted, tool=tool))
    return out


def render(r: dict[str, Any]) -> str:
    lines = [f"endpoint     {r['endpoint_host']}",
             f"model        {r['model_ref']}",
             f"model_egress {r['model_egress']}   (grant and task spec must list exactly this)"]
    for name in ("start", "chain"):
        s = r["steps"].get(name)
        if s is None:
            lines.append(f"{name:<12} not run")
        elif s["ok"]:
            lines.append(f"{name:<12} ok  output={','.join(map(str, s['output_types'])) or '-'}"
                         f"  tokens={s.get('total_tokens')}" + (f"  image={s['image']}" if s.get("image") else ""))
        else:
            lines.append(f"{name:<12} FAILED {s['error_code']}" + (f"  {s['detail']}" if s.get("detail") else ""))
    if r["ok"]:
        if r.get("history") == "client":
            lines.append("history      client (the endpoint refused previous_response_id; the runtime resends the "
                         "conversation, last screenshots only)")
        lines.append("MODEL CHECK OK")
    else:
        lines.append(f"MODEL CHECK FAILED: {r['error_code']}" + (f" - {r['hint']}" if r.get("hint") else ""))
    return "\n".join(lines)
