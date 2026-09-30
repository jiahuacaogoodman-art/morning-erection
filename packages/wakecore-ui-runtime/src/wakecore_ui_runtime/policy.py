"""Origin allow-listing and credential-field detection shared by observer and CU loop."""
import re
from typing import Iterable, Optional
from urllib.parse import urlsplit

_DEFAULT = {"http": 80, "https": 443}
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SESSION_REF = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
INTERNAL_SCHEMES = ("data:", "blob:", "about:")


def origin(url: str) -> Optional[str]:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT or not parts.hostname or "@" in parts.netloc or "\\" in url:
        return None
    host = parts.hostname.lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != _DEFAULT[parts.scheme] else "")


def normalise(origins: Iterable[str]) -> frozenset[str]:
    return frozenset(o for o in (origin(x) for x in origins) if o)


def in_scope(url: str, allowed: frozenset[str]) -> bool:
    o = origin(url)
    return o is not None and o in allowed


# Runs in the page: is the focused element a credential field (password / one-time code)?
SENSITIVE_FOCUS_JS = """() => {
  let e = document.activeElement;
  while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
  if (!e || e === document.body) return null;
  const attr = n => (e.getAttribute(n) || '').toLowerCase();
  const ident = (attr('name') + ' ' + attr('id') + ' ' + attr('placeholder') + ' ' + attr('aria-label'));
  const sensitive = attr('type') === 'password' || /one-time-code|password/.test(attr('autocomplete'))
    || /otp|totp|mfa|passcode|passwd|password|验证码|口令|密码/.test(ident);
  return {tag: e.tagName, sensitive};
}"""
