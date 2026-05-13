"""
Low-level JWT utilities for fastapi-mvt.

This private module holds the Auth class, TokenBlacklist, and the small
password helpers so that other auth sub-modules can import them without
triggering a circular-import through fastapi_mvt.auth.__init__.
"""

import bcrypt as _bcrypt
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials


# ── Password helpers ──────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Hash a plain password with bcrypt."""
    password_bytes = password.encode("utf-8")[:72]
    return _bcrypt.hashpw(password_bytes, _bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Return True if *plain_password* matches *hashed_password*."""
    password_bytes = plain_password.encode("utf-8")[:72]
    return _bcrypt.checkpw(password_bytes, hashed_password.encode("utf-8"))


# ── Token blacklist ───────────────────────────────────────────────────────────

class TokenBlacklist:
    """
    In-memory store for revoked JWT JTI claims.

    Revoked entries are automatically pruned when they pass their natural
    expiry, keeping memory usage bounded.

    Note: process-local. Use a shared store (Redis, DB) for multi-process
    deployments.
    """

    def __init__(self) -> None:
        self._revoked: Dict[str, datetime] = {}

    def revoke(self, jti: str, expires_at: datetime) -> None:
        self._cleanup()
        self._revoked[jti] = expires_at

    def is_revoked(self, jti: str) -> bool:
        expires_at = self._revoked.get(jti)
        if expires_at is None:
            return False
        if datetime.utcnow() >= expires_at:
            del self._revoked[jti]
            return False
        return True

    def _cleanup(self) -> None:
        now = datetime.utcnow()
        expired = [jti for jti, exp in self._revoked.items() if now >= exp]
        for jti in expired:
            del self._revoked[jti]


# Module-level singleton shared across all Auth instances in this process.
token_blacklist = TokenBlacklist()

# HTTP Bearer security scheme used by dependencies.
security = HTTPBearer()


# ── Auth class ────────────────────────────────────────────────────────────────

class Auth:
    """
    JWT Authentication helper.

    Handles token encoding, decoding, revocation, and the simple
    user-ID extraction needed by the dependency system.

    Example::

        auth = Auth(secret_key="super-secret")
        token = auth.create_access_token(subject="42")
        payload = auth.decode(token)
    """

    def __init__(
        self,
        secret_key: str,
        algorithm: str = "HS256",
        access_token_expire_minutes: int = 30,
    ):
        self.secret_key = secret_key
        self.algorithm = algorithm
        self.access_token_expire_minutes = access_token_expire_minutes

    def encode(
        self,
        data: Dict[str, Any],
        expires_delta: Optional[timedelta] = None,
    ) -> str:
        """Encode *data* into a signed JWT string."""
        to_encode = data.copy()
        to_encode.setdefault("jti", str(uuid.uuid4()))

        expire = datetime.utcnow() + (
            expires_delta
            if expires_delta
            else timedelta(minutes=self.access_token_expire_minutes)
        )
        to_encode["exp"] = expire
        return jwt.encode(to_encode, self.secret_key, algorithm=self.algorithm)

    def decode(self, token: str) -> Dict[str, Any]:
        """
        Decode and validate a JWT string.

        Raises :class:`fastapi.HTTPException` (401) for invalid or
        revoked tokens.
        """
        try:
            payload = jwt.decode(
                token, self.secret_key, algorithms=[self.algorithm]
            )
        except JWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Could not validate credentials: {exc}",
                headers={"WWW-Authenticate": "Bearer"},
            )

        jti = payload.get("jti")
        if jti and token_blacklist.is_revoked(jti):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has been revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return payload

    def create_access_token(
        self,
        subject: str,
        additional_claims: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Create a short-lived access token for *subject* (usually user id)."""
        claims: Dict[str, Any] = {"sub": str(subject)}
        if additional_claims:
            claims.update(additional_claims)
        return self.encode(claims)

    def revoke(self, token: str) -> None:
        """
        Revoke a JWT by blacklisting its JTI.

        Silently ignores already-invalid tokens.
        """
        try:
            payload = jwt.decode(
                token, self.secret_key, algorithms=[self.algorithm]
            )
        except JWTError:
            return
        jti = payload.get("jti")
        exp = payload.get("exp")
        if jti and exp:
            token_blacklist.revoke(jti, datetime.utcfromtimestamp(exp))

    def get_current_user_id(self, token: str) -> str:
        """Extract the ``sub`` claim from a token (legacy helper)."""
        payload = self.decode(token)
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user_id


# ── Legacy dependency factory (kept for backward compatibility) ───────────────

def get_auth_dependency(auth_instance: Auth):
    """
    Factory that creates a FastAPI dependency returning the user-id string.

    Kept for backward compatibility. New code should use
    :func:`fastapi_mvt.auth.dependencies.get_current_user` instead.

    Example::

        auth = Auth(secret_key="secret")
        get_user_id = get_auth_dependency(auth)

        @app.get("/protected")
        async def protected(user_id: str = Depends(get_user_id)):
            return {"user_id": user_id}
    """

    async def _dependency(
        credentials: HTTPAuthorizationCredentials = Depends(security),
    ) -> str:
        return auth_instance.get_current_user_id(credentials.credentials)

    return _dependency


__all__ = [
    "Auth",
    "TokenBlacklist",
    "token_blacklist",
    "security",
    "hash_password",
    "verify_password",
    "get_auth_dependency",
]
