"""
Pydantic schemas for the built-in auth endpoints.

These are the request/response models for:
  POST /auth/register
  POST /auth/login
  POST /auth/logout
  POST /auth/refresh
  GET  /auth/me

Developers who need a richer /auth/me response should supply a
``me_serializer`` in AuthConfig rather than changing these schemas.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, field_validator


# ── Request schemas ───────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    """Payload for POST /auth/register."""

    email: str
    password: str

    @field_validator("email")
    @classmethod
    def email_must_contain_at(cls, v: str) -> str:
        if "@" not in v:
            raise ValueError("Enter a valid email address")
        return v.lower().strip()

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class LoginRequest(BaseModel):
    """Payload for POST /auth/login."""

    email: str
    password: str

    @field_validator("email")
    @classmethod
    def normalise_email(cls, v: str) -> str:
        return v.lower().strip()


class RefreshRequest(BaseModel):
    """Payload for POST /auth/refresh."""

    refresh_token: str


# ── Response schemas ──────────────────────────────────────────────────────────

class TokenResponse(BaseModel):
    """Token pair returned after register / login / refresh.

    ``refresh_token`` is ``None`` when ``AuthConfig.enable_refresh_token=False``.
    """

    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"


class MeResponse(BaseModel):
    """
    Default response for GET /auth/me.

    Override by providing ``AuthConfig.me_serializer`` — the serializer
    can return any dict or Pydantic model; the endpoint passes it through
    directly::

        def teacher_me(user):
            return {
                "id": user.id,
                "email": user.email,
                "profile": {"role": "teacher", "full_name": user.full_name},
            }

        configure_auth(AuthConfig(..., me_serializer=teacher_me))
    """

    id: int
    email: str
    is_active: bool

    model_config = {"from_attributes": True}


__all__ = [
    "RegisterRequest",
    "LoginRequest",
    "RefreshRequest",
    "TokenResponse",
    "MeResponse",
]
