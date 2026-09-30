"""Web origins for resource scopes (V0.3 P0).

The kernel must not import urllib/http (architecture rule), so this is a deliberately
strict, tiny parser: only absolute http(s) URLs, no userinfo, no whitespace/control
characters. Anything it cannot parse unambiguously has *no* origin and is therefore never
inside a scope. `https://allowed.example@evil.example/` is rejected rather than guessed.
"""
from typing import Any, Iterable, Optional

_DEFAULT_PORTS = {"http": "80", "https": "443"}
_HOST_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-.")


def origin_of(url: Any) -> Optional[str]:
    if not isinstance(url, str) or not url or any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in url):
        return None
    scheme, sep, rest = url.partition("://")
    scheme = scheme.lower()
    if not sep or scheme not in _DEFAULT_PORTS:
        return None
    authority = rest
    for stop in "/?#":
        authority = authority.split(stop, 1)[0]
    if not authority or "@" in authority:
        return None
    if authority.startswith("["):  # IPv6 literal
        host, bracket, tail = authority[1:].partition("]")
        if not bracket or not host or not all(c in "0123456789abcdefABCDEF:." for c in host):
            return None
        host = "[" + host.lower() + "]"
        port = tail[1:] if tail.startswith(":") else ("" if not tail else None)
    else:
        host, _, port = authority.partition(":")
        host = host.lower().rstrip(".")
        if not host or not set(host) <= _HOST_CHARS or ".." in host or host.startswith((".", "-")):
            return None
    if port is None or (port and (not port.isdigit() or not 0 < int(port) < 65536)):
        return None
    if port == _DEFAULT_PORTS[scheme]:
        port = ""
    return f"{scheme}://{host}" + (f":{int(port)}" if port else "")


def normalise_origins(origins: Iterable[Any]) -> list[str]:
    out = {o for o in (origin_of(x) for x in origins) if o is not None}
    return sorted(out)


def url_in_scope(url: Any, origins: Iterable[Any]) -> bool:
    o = origin_of(url)
    return o is not None and o in set(normalise_origins(origins))


def dotted(payload: Any, path: str) -> Any:
    cur = payload
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur
