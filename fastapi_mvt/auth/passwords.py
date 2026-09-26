"""Password hashing.

Hashes use Django's format (``pbkdf2_sha256$iterations$salt$hash``), so users
migrated from a Django project can log in unchanged. Hashing is deliberately
slow, so the async helpers run it in a worker thread instead of blocking the
event loop. Hashes from fastapi-mvt 0.1 (bcrypt) are still accepted when the
``bcrypt`` package is installed, and are upgraded on the next login.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import secrets

import anyio

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 870_000
UNUSABLE_PREFIX = "!"


def make_password(password: str, *, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_urlsafe(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"{ALGORITHM}${iterations}${salt}${base64.b64encode(digest).decode('ascii')}"


def check_password(password: str, encoded: str) -> bool:
    if not encoded or encoded.startswith(UNUSABLE_PREFIX):
        return False
    if encoded.startswith(("$2a$", "$2b$", "$2y$")):
        return _check_bcrypt(password, encoded)
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
    except ValueError:
        return False
    if algorithm != ALGORITHM:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
    return hmac.compare_digest(base64.b64encode(digest).decode("ascii"), expected)


def needs_rehash(encoded: str) -> bool:
    if encoded.startswith(("$2a$", "$2b$", "$2y$")):
        return True
    parts = encoded.split("$")
    return len(parts) == 4 and parts[0] == ALGORITHM and int(parts[1]) < ITERATIONS


def _check_bcrypt(password: str, encoded: str) -> bool:
    try:
        import bcrypt
    except ImportError:
        return False
    return bcrypt.checkpw(password.encode("utf-8")[:72], encoded.encode("utf-8"))


async def amake_password(password: str) -> str:
    return await anyio.to_thread.run_sync(make_password, password)


async def acheck_password(password: str, encoded: str) -> bool:
    return await anyio.to_thread.run_sync(check_password, password, encoded)


@functools.lru_cache(maxsize=1)
def dummy_hash() -> str:
    """Checked when an email is unknown, so response time does not reveal which emails exist."""
    return make_password(secrets.token_urlsafe(16))
