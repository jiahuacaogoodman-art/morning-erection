"""RFC 6238 TOTP (HMAC-SHA1, 30 s, 6 digits): the portal's MFA and the user's authenticator app."""
import base64
import hmac
import struct
import time
from hashlib import sha1
from typing import Optional


def totp(secret_b32: str, at: Optional[float] = None, *, step: int = 30, digits: int = 6) -> str:
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    counter = int((time.time() if at is None else at) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def verify(secret_b32: str, code: str, *, window: int = 1) -> bool:
    now = time.time()
    return any(hmac.compare_digest(totp(secret_b32, now + d * 30), code) for d in range(-window, window + 1))
