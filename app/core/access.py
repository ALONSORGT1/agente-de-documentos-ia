"""Signed, expiring session cookies; the shared key never enters browser storage."""
from hashlib import sha256
import hmac
import secrets
import time

COOKIE_NAME = "document_agent_session"
SESSION_SECONDS = 6 * 60 * 60


def create_session(key):
    value = f"{int(time.time()) + SESSION_SECONDS}.{secrets.token_hex(16)}"
    signature = hmac.new(key.encode(), ("document-session:" + value).encode(), sha256).hexdigest()
    return value + "." + signature


def authenticated(request, key):
    if not key:
        return False
    supplied = request.headers.get("X-App-Key", "")
    if supplied and secrets.compare_digest(supplied.encode(), key.encode()):
        return True
    cookie = request.cookies.get(COOKIE_NAME, "")
    try:
        expires, nonce, signature = cookie.split(".")
        remaining = int(expires) - time.time()
        if not 0 < remaining <= SESSION_SECONDS or len(nonce) != 32:
            return False
        value = expires + "." + nonce
        expected = hmac.new(key.encode(), ("document-session:" + value).encode(), sha256).hexdigest()
        return secrets.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False
