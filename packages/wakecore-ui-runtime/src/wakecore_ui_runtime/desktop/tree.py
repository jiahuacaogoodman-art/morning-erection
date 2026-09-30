"""Parse the accessibility tree that the macOS Computer Use bridge returns from get_app_state.

The text looks like this (roles are localised, element indices are only valid in the session
that produced them):

    <app_state>
    App=/System/Applications/Calculator.app/ (bundleID com.apple.calculator, pid 58016)
    Window: "计算器", App: 计算器.
    0 标准窗口 计算器, ID: main, Secondary Actions: Raise
    \t1 分离组 main, SidebarNavigationSplitView
    \t\t3 滚动区 Description: 编辑字段, ID: StandardInputView, Secondary Actions: 拷贝
    \t\t\t4 文本 3
    \t\t\t54 按钮 Description: 等于, ID: Equals
    \t59 缩放按钮 (disabled)
    </app_state>

One line per element: tabs (depth), index, a head ("<role> <label>", both free text), then
known attributes. Attribute values may themselves contain ": " (`ID: Mode: scientific`), so
attributes are split only in front of a *known* key. Bidi marks (U+200E etc.) are stripped.
A line that does not start with an index continues the previous element's last value (a
multi-line text value).

Deterministic and pure: extractors and the act loop's guards read this, never the screenshot.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Optional

KEYS = ("Description", "Help", "Value", "ID", "Secondary Actions")
_ATTR_KEY = {"Description": "description", "Help": "help", "Value": "value", "ID": "id",
             "Secondary Actions": "actions"}
_SPLIT = re.compile(r"(?:, | )(?=(?:" + "|".join(re.escape(k) for k in KEYS) + r"): )")
_LINE = re.compile(r"^(\t*)(\d+) ?(.*)$")
_APP = re.compile(r"^App=(.*?) \(bundleID ([^,]+), pid (\d+)\)\s*$")
_WINDOW = re.compile(r'^Window: "(.*)", App: (.*?)\.?\s*$')
_BIDI = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_DISABLED = " (disabled)"

# Credential inputs: never typed into, their values never shown to the model. Matched against
# the head, description, help and id of an element (case-insensitive).
SENSITIVE = re.compile(r"password|passcode|passwort|mot de passe|secure|one[- ]time|\botp\b|2fa|"
                       r"verification code|security code|\bpin\b|密码|口令|验证码|校验码|安全码|动态码|安全文本",
                       re.IGNORECASE)


class TreeError(ValueError):
    pass


@dataclass
class Node:
    index: int
    depth: int
    head: str
    description: Optional[str] = None
    help: Optional[str] = None
    value: Optional[str] = None
    id: Optional[str] = None
    actions: list[str] = field(default_factory=list)
    disabled: bool = False
    parent: Optional[int] = None          # position in AppState.nodes, not the element index
    pos: int = 0

    def attr(self, name: str) -> Optional[str]:
        return {"head": self.head, "description": self.description, "help": self.help, "value": self.value,
                "id": self.id}.get(name)

    @property
    def sensitive(self) -> bool:
        return any(v and SENSITIVE.search(v) for v in (self.head, self.description, self.help, self.id))

    def as_dict(self) -> dict[str, Any]:
        d = {"index": self.index, "depth": self.depth, "head": self.head}
        for k in ("description", "help", "value", "id"):
            if getattr(self, k) is not None:
                d[k] = getattr(self, k)
        if self.actions:
            d["actions"] = self.actions
        if self.disabled:
            d["disabled"] = True
        return d


@dataclass
class AppState:
    app_path: Optional[str]
    bundle_id: Optional[str]
    pid: Optional[int]
    window: Optional[str]
    nodes: list[Node]

    def by_index(self, index: int) -> Optional[Node]:
        for n in self.nodes:
            if n.index == index:
                return n
        return None

    def ancestors(self, n: Node) -> list[Node]:
        out = []
        while n.parent is not None:
            n = self.nodes[n.parent]
            out.append(n)
        return out

    def descendants(self, n: Node) -> list[Node]:
        out = []
        for m in self.nodes[n.pos + 1:]:
            if m.depth <= n.depth:
                break
            out.append(m)
        return out

    def has_sensitive(self) -> bool:
        return any(n.sensitive for n in self.nodes)

    def render(self) -> str:
        """The tree as the model sees it: the same line format, values of credential-looking
        elements replaced, so a password manager's autofill never reaches the model."""
        lines = []
        if self.app_path or self.bundle_id:
            lines.append(f"App={self.app_path} (bundleID {self.bundle_id}, pid {self.pid})")
        if self.window is not None:
            lines.append(f'Window: "{self.window}"')
        for n in self.nodes:
            parts = [f"{n.index} {n.head}".rstrip()]
            for key in KEYS:
                attr = _ATTR_KEY[key]
                v = getattr(n, attr)
                if attr == "actions":
                    if v:
                        parts.append(f"{key}: {', '.join(v)}")
                elif v is not None:
                    if attr == "value" and n.sensitive:
                        v = "[hidden]"
                    parts.append(f"{key}: {v}")
            line = parts[0] + ("" if len(parts) == 1 else " " + ", ".join(parts[1:]))
            lines.append("\t" * n.depth + line + (_DISABLED if n.disabled else ""))
        return "\n".join(lines)


def _clean(s: str) -> str:
    return _BIDI.sub("", s)


def _parse_body(rest: str) -> dict[str, Any]:
    out: dict[str, Any] = {"disabled": False}
    if rest.endswith(_DISABLED):
        out["disabled"], rest = True, rest[:-len(_DISABLED)]
    elif rest == _DISABLED.strip():
        out["disabled"], rest = True, ""
    pieces = _SPLIT.split(rest)
    head = pieces[0]
    if any(head.startswith(k + ": ") for k in KEYS):      # no head at all: "Description: x, ID: y"
        head, attrs = "", pieces
    else:
        attrs = pieces[1:]
    out["head"] = head.strip()
    for piece in attrs:
        key, _, value = piece.partition(": ")
        attr = _ATTR_KEY[key]
        out[attr] = [a.strip() for a in value.split(", ") if a.strip()] if attr == "actions" else value
    return out


def parse(text: str) -> AppState:
    if not isinstance(text, str):
        raise TreeError("app state is not text")
    text = _clean(text)
    start, end = text.find("<app_state>"), text.find("</app_state>")
    if start < 0:
        raise TreeError("no <app_state> in the bridge's answer")
    body = text[start + len("<app_state>"):end if end > start else len(text)]
    app_path = bundle = window = None
    pid: Optional[int] = None
    nodes: list[Node] = []
    stack: list[Node] = []
    for raw in body.split("\n"):
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        m = _LINE.match(line)
        if m is None:
            if not nodes:
                a, w = _APP.match(line), _WINDOW.match(line)
                if a:
                    app_path, bundle, pid = a.group(1), a.group(2).strip(), int(a.group(3))
                elif w:
                    window = w.group(1)
                continue
            last = nodes[-1]                                # continuation of a multi-line value
            for attr in ("value", "description", "help"):
                if getattr(last, attr) is not None:
                    setattr(last, attr, getattr(last, attr) + "\n" + line.strip("\t"))
                    break
            else:
                last.head += "\n" + line.strip("\t")
            continue
        depth, index = len(m.group(1)), int(m.group(2))
        while stack and stack[-1].depth >= depth:
            stack.pop()
        node = Node(index=index, depth=depth, parent=stack[-1].pos if stack else None, pos=len(nodes),
                    **_parse_body(m.group(3)))
        nodes.append(node)
        stack.append(node)
    return AppState(app_path=app_path, bundle_id=bundle, pid=pid, window=window, nodes=nodes)
