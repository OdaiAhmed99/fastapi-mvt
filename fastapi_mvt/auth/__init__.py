"""
fastapi_mvt.auth — public API for the built-in authentication system.

Quick start
-----------
1. Configure once at app startup::

       from fastapi_mvt.auth import configure_auth, AuthConfig, auth_router

       configure_auth(AuthConfig(secret_key="change-me"))
       app.include_router(auth_router)

2. Protect any route::

       from fastapi import Depends
       from fastapi_mvt.auth import get_current_user

       @router.get("/dashboard")
       def dashboard(user = Depends(get_current_user)):
           return {"id": user.id}

3. Extend the user model (zero extra tables)::

       # accounts/models.py
       from fastapi_mvt.auth import AbstractAuthUser
       from fastapi_mvt.db import Base
       from sqlalchemy import Column, String

       class User(Base, AbstractAuthUser):
           __tablename__ = "users"
           full_name = Column(String(120))

       # In startup:
       configure_auth(AuthConfig(secret_key=..., user_model="accounts.models.User"))

Module layout
-------------
_jwt.py         — Auth class, TokenBlacklist, password helpers (private)
config.py       — AuthConfig, configure_auth, get_auth_config, get_user_model
models.py       — AbstractAuthUser, DefaultAuthUser
schemas.py      — Pydantic request/response schemas
dependencies.py — get_current_user FastAPI dependency
router.py       — auth_router (register / login / logout / refresh / me)
"""

# ── Low-level JWT utilities (backward-compatible re-exports) ──────────────────
from fastapi_mvt.auth._jwt import (
    Auth,
    TokenBlacklist,
    token_blacklist,
    security,
    hash_password,
    verify_password,
    get_auth_dependency,
)

# ── Auth backends ─────────────────────────────────────────────────────────────
from fastapi_mvt.auth.backends import AuthBackend, JWTAuthBackend

# ── Configuration ─────────────────────────────────────────────────────────────
from fastapi_mvt.auth.config import (
    AuthConfig,
    configure_auth,
    get_auth_config,
    get_backend,
    get_user_model,
)

# ── Models ────────────────────────────────────────────────────────────────────
from fastapi_mvt.auth.models import AbstractAuthUser, DefaultAuthUser

# ── Schemas ───────────────────────────────────────────────────────────────────
from fastapi_mvt.auth.schemas import (
    RegisterRequest,
    LoginRequest,
    RefreshRequest,
    TokenResponse,
    MeResponse,
)

# ── Dependency ────────────────────────────────────────────────────────────────
from fastapi_mvt.auth.dependencies import get_current_user

# ── Router ────────────────────────────────────────────────────────────────────
from fastapi_mvt.auth.router import router as auth_router


__all__ = [
    # JWT core
    "Auth",
    "TokenBlacklist",
    "token_blacklist",
    "security",
    "hash_password",
    "verify_password",
    "get_auth_dependency",
    # Backends
    "AuthBackend",
    "JWTAuthBackend",
    # Config
    "AuthConfig",
    "configure_auth",
    "get_auth_config",
    "get_backend",
    "get_user_model",
    # Models
    "AbstractAuthUser",
    "DefaultAuthUser",
    # Schemas
    "RegisterRequest",
    "LoginRequest",
    "RefreshRequest",
    "TokenResponse",
    "MeResponse",
    # Dependency
    "get_current_user",
    # Router
    "auth_router",
]


