"""
FastAPI dependencies for the built-in auth system.

Public API
----------
get_current_user
    Validates the Bearer token and returns the authenticated user object.
    Use this wherever you need to know who is making the request.

_get_db (internal)
    Lazily resolves a SQLAlchemy session from AuthConfig.db_dependency or
    via auto-discovery.  Used by the auth router and get_current_user.

Usage in your routes::

    from fastapi import Depends
    from fastapi_mvt.auth import get_current_user

    @router.get("/posts")
    def list_posts(current_user = Depends(get_current_user)):
        return db.query(Post).filter(Post.author_id == current_user.id).all()
"""

from __future__ import annotations

from typing import Any, Generator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials

from fastapi_mvt.auth._jwt import security
from fastapi_mvt.auth.config import get_auth_config, get_backend, get_user_model


# ── Internal DB resolver ──────────────────────────────────────────────────────

def _get_db() -> Generator:
    """
    Internal dependency: yields a SQLAlchemy session at request time.

    Resolution order:
      1. ``AuthConfig.db_dependency`` — explicit project ``get_db`` function.
      2. Auto-discovery via :func:`fastapi_mvt.utils.project.get_db_session`.

    Developers should prefer the explicit path for clarity::

        from myproject.db import get_db
        configure_auth(AuthConfig(secret_key=..., db_dependency=get_db))
    """
    config = get_auth_config()
    if config.db_dependency is not None:
        yield from config.db_dependency()
    else:
        from fastapi_mvt.utils.project import get_db_session
        db_fn = get_db_session()
        yield from db_fn()


# ── Public dependency ─────────────────────────────────────────────────────────

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Any = Depends(_get_db),
) -> Any:
    """
    FastAPI dependency — validate the Bearer token and return the active user.

    Behaviour
    ---------
    * Decodes and verifies the JWT (signature, expiry, blacklist).
    * Rejects refresh tokens used where an access token is expected.
    * Resolves the user via ``AuthConfig.current_user_loader`` when set,
      otherwise queries the configured user model directly.
    * Raises 401 if the user is not found.
    * Raises 403 if ``user.is_active`` is False.

    Returns the user *model instance* (DefaultAuthUser or your custom model),
    so callers always get ``current_user.id``, ``current_user.email``, etc.

    Business apps that need role / profile data should add a project-level
    dependency on top of this one (e.g. ``get_current_actor``) rather than
    modifying auth logic.

    Example::

        @router.get("/dashboard")
        def dashboard(current_user = Depends(get_current_user)):
            return {"welcome": current_user.email}
    """
    config = get_auth_config()
    backend = get_backend()

    token = credentials.credentials
    # decode_access_token raises 401 for invalid/expired tokens and also
    # rejects refresh tokens used in place of access tokens.
    payload = backend.decode_access_token(token)

    user_id = payload.get(config.token_user_id_field)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Resolve user — custom loader takes priority.
    if config.current_user_loader is not None:
        user = config.current_user_loader(user_id, db)
    else:
        UserModel = get_user_model()
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid user identifier in token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = db.query(UserModel).filter(UserModel.id == uid).first()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is inactive",
        )

    return user


__all__ = ["get_current_user", "_get_db"]
