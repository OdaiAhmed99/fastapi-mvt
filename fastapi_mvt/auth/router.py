"""
Built-in auth router for fastapi-mvt.

Endpoints
---------
POST /auth/register  — create an account, return token pair
POST /auth/login     — authenticate, return token pair
POST /auth/logout    — revoke current access token + clear cookie
POST /auth/refresh   — exchange a refresh token for a new access token
GET  /auth/me        — return the authenticated user's identity

Include this router in your FastAPI application::

    from fastapi import FastAPI
    from fastapi_mvt.auth import auth_router

    app = FastAPI()
    app.include_router(auth_router)

All endpoints read configuration from the active :class:`AuthConfig` at
request time, so they adapt automatically when you call ``configure_auth``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import OperationalError as _SAOperationalError

from fastapi_mvt.auth._jwt import security
from fastapi_mvt.auth.config import get_auth_config, get_backend, get_user_model
from fastapi_mvt.auth.dependencies import _get_db, get_current_user
from fastapi_mvt.auth.schemas import (
    LoginRequest,
    MeResponse,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _check_migration_error(exc: _SAOperationalError) -> None:
    """
    Re-raise *exc* as an HTTP 500 with a developer-friendly message when the
    underlying cause is a missing table (i.e. migrations have not been run).
    """
    msg = str(exc.orig).lower()
    if "no such table" in msg or "relation" in msg and "does not exist" in msg:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Database table not found. "
                "Run 'python manage.py migrate' to create all required tables, "
                "then restart the server."
            ),
        )
    raise exc  # unrelated DB error — propagate normally


# ── Internal helpers ──────────────────────────────────────────────────────────

def _build_tokens(user_id: int) -> TokenResponse:
    """
    Issue an access + refresh token pair for *user_id* via the active backend.
    """
    cfg = get_auth_config()
    backend = get_backend()

    extra: dict = {}
    if cfg.token_payload_builder:
        extra = cfg.token_payload_builder(user_id) or {}

    access_token = backend.create_access_token(user_id, extra or None)
    refresh_token = backend.create_refresh_token(user_id) if cfg.enable_refresh_token else None

    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


def _set_cookie(response: Response, token: str) -> None:
    """Write an HTTP-only cookie carrying the access token."""
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        # Set secure=True in production when behind HTTPS.
        secure=False,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user account",
)
def register(
    payload: RegisterRequest,
    response: Response,
    db: Any = Depends(_get_db),
) -> TokenResponse:
    """
    Create a new user and return an access + refresh token pair.

    Returns 409 if a user with the same email already exists.
    """
    cfg = get_auth_config()

    # Delegate to a project-supplied handler when one is configured.
    if cfg.register_handler is not None:
        return cfg.register_handler(payload, response, db)

    UserModel = get_user_model()

    try:
        existing = db.query(UserModel).filter(UserModel.email == payload.email).first()
    except _SAOperationalError as exc:
        _check_migration_error(exc)

    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email address already exists",
        )

    backend = get_backend()
    user = UserModel(
        email=payload.email,
        hashed_password=backend.hash_password(payload.password),
    )
    try:
        db.add(user)
        db.commit()
        db.refresh(user)
    except _SAOperationalError as exc:
        db.rollback()
        _check_migration_error(exc)

    tokens = _build_tokens(user.id)
    _set_cookie(response, tokens.access_token)
    return tokens


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Authenticate and get tokens",
)
def login(
    payload: LoginRequest,
    response: Response,
    db: Any = Depends(_get_db),
) -> TokenResponse:
    """
    Verify credentials and return an access + refresh token pair.

    Returns 401 for wrong credentials and 403 for inactive accounts.
    """
    UserModel = get_user_model()

    try:
        user = db.query(UserModel).filter(UserModel.email == payload.email).first()
    except _SAOperationalError as exc:
        _check_migration_error(exc)

    backend = get_backend()
    if not user or not backend.verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is inactive",
        )

    tokens = _build_tokens(user.id)
    _set_cookie(response, tokens.access_token)
    return tokens


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the current access token",
)
def logout(
    response: Response,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> None:
    """
    Blacklist the presented access token and delete the auth cookie.

    The token remains cryptographically valid until its ``exp`` claim, but
    the blacklist check in ``Auth.decode`` will reject it on every subsequent
    request.
    """
    backend = get_backend()
    backend.revoke_token(credentials.credentials)
    response.delete_cookie("access_token")


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Get a new access token using a refresh token",
)
def refresh(payload: RefreshRequest, response: Response) -> TokenResponse:
    """
    Exchange a valid refresh token for a fresh access + refresh token pair.

    Returns 404 if ``AuthConfig.enable_refresh_token`` is ``False``.
    Returns 400 if the token is not a refresh token.
    Returns 401 if the token is expired or revoked.
    """
    cfg = get_auth_config()

    if not cfg.enable_refresh_token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Refresh tokens are disabled for this application",
        )

    backend = get_backend()

    # decode_refresh_token raises 400/401 on any problem.
    token_payload = backend.decode_refresh_token(payload.refresh_token)

    user_id = token_payload.get(cfg.token_user_id_field)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate refresh token",
        )

    tokens = _build_tokens(int(user_id))
    _set_cookie(response, tokens.access_token)
    return tokens


@router.get(
    "/me",
    summary="Get the authenticated user's identity",
)
def me(current_user: Any = Depends(get_current_user)) -> Any:
    """
    Return basic identity data for the authenticated user.

    Default response::

        {"id": 1, "email": "user@example.com", "is_active": true}

    Override by providing ``AuthConfig.me_serializer``::

        def custom_me(user):
            return {
                "id": user.id,
                "email": user.email,
                "profile": {"role": user.profile.role},
            }

        configure_auth(AuthConfig(..., me_serializer=custom_me))

    The serializer receives the full user model instance and can return any
    JSON-serialisable value.
    """
    cfg = get_auth_config()

    if cfg.me_serializer is not None:
        return cfg.me_serializer(current_user)

    return MeResponse(
        id=current_user.id,
        email=current_user.email,
        is_active=current_user.is_active,
    )


__all__ = ["router"]
