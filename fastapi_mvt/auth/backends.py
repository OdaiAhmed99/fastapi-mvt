"""
Auth backends for fastapi-mvt.

An *auth backend* is the object responsible for token creation, token
validation, password hashing, and token revocation.  By default the library
uses :class:`JWTAuthBackend`, but you can swap it out completely.

Implementing a custom backend
------------------------------
Subclass :class:`AuthBackend` and implement every abstract method::

    from fastapi_mvt.auth.backends import AuthBackend
    from fastapi_mvt.auth import configure_auth, AuthConfig

    class APIKeyBackend(AuthBackend):
        # Store issued keys in-memory (use Redis / DB in production)
        _keys: dict[str, int] = {}     # token → user_id

        def create_access_token(self, user_id: int, extra: dict | None = None) -> str:
            import secrets
            key = secrets.token_urlsafe(32)
            self._keys[key] = user_id
            return key

        def create_refresh_token(self, user_id: int) -> str:
            return self.create_access_token(user_id)   # no refresh concept

        def decode_access_token(self, token: str) -> dict:
            user_id = self._keys.get(token)
            if user_id is None:
                from fastapi import HTTPException, status
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
            return {"sub": str(user_id)}

        def decode_refresh_token(self, token: str) -> dict:
            return self.decode_access_token(token)

        def revoke_token(self, token: str) -> None:
            self._keys.pop(token, None)

        def hash_password(self, plain: str) -> str:
            from fastapi_mvt.auth._jwt import hash_password
            return hash_password(plain)

        def verify_password(self, plain: str, hashed: str) -> bool:
            from fastapi_mvt.auth._jwt import verify_password
            return verify_password(plain, hashed)

    configure_auth(AuthConfig(
        secret_key="unused-for-api-keys",
        auth_backend=APIKeyBackend(),
    ))

Commitments
-----------
Your backend **must** honour these contracts so the built-in router and the
``get_current_user`` dependency keep working without modification:

* ``decode_access_token`` raises ``HTTPException(401)`` on any invalid token.
* ``decode_refresh_token`` raises ``HTTPException(400)`` when the token is not
  a valid refresh token, and ``HTTPException(401)`` when it is expired/revoked.
* Both decode methods return a ``dict`` that contains ``"sub"`` mapped to the
  user's primary-key as a **string** (``"42"``, not ``42``).
* ``hash_password`` / ``verify_password`` use the same algorithm so passwords
  stored by register still work at login.
"""

from __future__ import annotations

import abc
from datetime import timedelta
from typing import Any, Optional


class AuthBackend(abc.ABC):
    """
    Abstract base class for auth backends.

    Subclass this to replace JWT with any token mechanism you like
    (API keys, opaque tokens, sessions, OAuth2 introspection, …).

    All methods are synchronous.  If you need async behaviour (e.g. Redis
    token lookup), run the async work via ``asyncio.run()`` or push it to a
    background task — FastAPI dependencies that call the backend are sync.
    """

    @abc.abstractmethod
    def create_access_token(self, user_id: int, extra: Optional[dict] = None) -> str:
        """
        Create and return a new access token for *user_id*.

        Parameters
        ----------
        user_id:
            The integer primary key of the user.
        extra:
            Optional extra claims / metadata to embed in the token.
            Content is backend-specific; the router passes
            ``AuthConfig.token_payload_builder(user_id)`` here when set.
        """

    @abc.abstractmethod
    def create_refresh_token(self, user_id: int) -> str:
        """
        Create and return a new refresh token for *user_id*.

        If your backend has no refresh concept, return an empty string and
        raise ``HTTPException(400)`` inside ``decode_refresh_token``.
        """

    @abc.abstractmethod
    def decode_access_token(self, token: str) -> dict:
        """
        Validate *token* and return its payload as a plain ``dict``.

        **Must** contain the key ``"sub"`` with the user's PK as a string.

        Raises
        ------
        fastapi.HTTPException(401)
            Token is invalid, expired, or revoked.
        """

    @abc.abstractmethod
    def decode_refresh_token(self, token: str) -> dict:
        """
        Validate *token* as a **refresh** token and return its payload.

        **Must** contain the key ``"sub"`` with the user's PK as a string.

        Raises
        ------
        fastapi.HTTPException(400)
            Token is not a refresh token.
        fastapi.HTTPException(401)
            Token is expired or revoked.
        """

    @abc.abstractmethod
    def revoke_token(self, token: str) -> None:
        """
        Permanently invalidate *token* so it cannot be used again.

        Called by ``POST /auth/logout``.  For stateless backends (pure JWT
        without a blacklist) this can be a no-op, but make sure
        ``decode_access_token`` rejects the token after revocation.
        """

    @abc.abstractmethod
    def hash_password(self, plain: str) -> str:
        """Return a secure hash of *plain*."""

    @abc.abstractmethod
    def verify_password(self, plain: str, hashed: str) -> bool:
        """Return ``True`` iff *plain* matches *hashed*."""


# ── Default backend ───────────────────────────────────────────────────────────

class JWTAuthBackend(AuthBackend):
    """
    Default backend — signs tokens with HMAC-SHA (via ``python-jose``) and
    bcrypt-hashes passwords.

    This is what the library uses when no ``auth_backend`` is set in
    :class:`~fastapi_mvt.auth.config.AuthConfig`.  You normally never
    instantiate it directly; it is created internally from the config.

    You can subclass it to tweak JWT behaviour without reimplementing
    password hashing::

        class MyJWTBackend(JWTAuthBackend):
            def create_access_token(self, user_id, extra=None):
                # add a fixed claim to every token
                extra = extra or {}
                extra["iss"] = "my-app"
                return super().create_access_token(user_id, extra)

        configure_auth(AuthConfig(
            secret_key=...,
            auth_backend=MyJWTBackend(...),
        ))
    """

    def __init__(
        self,
        secret_key: str,
        algorithm: str = "HS256",
        access_token_expire_minutes: int = 30,
        refresh_token_expire_days: int = 7,
    ) -> None:
        from fastapi_mvt.auth._jwt import Auth, TokenBlacklist

        self._auth = Auth(
            secret_key=secret_key,
            algorithm=algorithm,
            access_token_expire_minutes=access_token_expire_minutes,
        )
        self._refresh_delta = timedelta(days=refresh_token_expire_days)

    # ── Token creation ────────────────────────────────────────────────────────

    def create_access_token(self, user_id: int, extra: Optional[dict] = None) -> str:
        claims: dict[str, Any] = {"sub": str(user_id)}
        if extra:
            claims.update(extra)
        return self._auth.encode(claims)

    def create_refresh_token(self, user_id: int) -> str:
        return self._auth.encode(
            {"sub": str(user_id), "type": "refresh"},
            expires_delta=self._refresh_delta,
        )

    # ── Token validation ──────────────────────────────────────────────────────

    def decode_access_token(self, token: str) -> dict:
        """Decode and validate an access token.  Rejects refresh tokens."""
        from fastapi import HTTPException, status

        payload = self._auth.decode(token)   # raises 401 if invalid/expired/revoked
        if payload.get("type") == "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Cannot use a refresh token as an access token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return payload

    def decode_refresh_token(self, token: str) -> dict:
        """Decode and validate a refresh token.  Rejects access tokens."""
        from fastapi import HTTPException, status

        payload = self._auth.decode(token)   # raises 401 if invalid/expired/revoked
        if payload.get("type") != "refresh":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The provided token is not a refresh token",
            )
        return payload

    # ── Revocation ────────────────────────────────────────────────────────────

    def revoke_token(self, token: str) -> None:
        self._auth.revoke(token)

    # ── Password helpers ──────────────────────────────────────────────────────

    def hash_password(self, plain: str) -> str:
        from fastapi_mvt.auth._jwt import hash_password
        return hash_password(plain)

    def verify_password(self, plain: str, hashed: str) -> bool:
        from fastapi_mvt.auth._jwt import verify_password
        return verify_password(plain, hashed)
