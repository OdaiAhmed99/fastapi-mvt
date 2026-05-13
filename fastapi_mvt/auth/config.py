"""
Central auth configuration for fastapi-mvt.

Usage::

    from fastapi_mvt.auth import configure_auth, AuthConfig

    configure_auth(AuthConfig(
        secret_key="change-me-in-production",
        user_model="accounts.models.User",   # omit to use DefaultAuthUser
    ))

The config is stored globally and read at request time by every auth
component (dependencies, router, get_user_model, etc.).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_auth_config: Optional["AuthConfig"] = None


@dataclass
class AuthConfig:
    """
    All knobs for the built-in auth system.

    Required
    --------
    secret_key
        The secret used to sign JWTs. **Must** be set; keep it out of VCS.

    Common overrides
    ----------------
    user_model
        Dotted path to the active user model class.
        Defaults to the library's own ``DefaultAuthUser``.
        Example: ``"accounts.models.User"``

    me_serializer
        Callable ``(user) -> dict`` used by ``GET /auth/me``.
        Lets you add profile data without modifying the auth endpoint::

            def my_me(user):
                return {"id": user.id, "email": user.email,
                        "full_name": user.full_name}

            AuthConfig(secret_key=..., me_serializer=my_me)

    current_user_loader
        Callable ``(user_id: str, db) -> user`` that replaces the default
        DB look-up inside ``get_current_user``.  Use it to preload relations
        or apply project-specific caching::

            def load_user(user_id, db):
                return db.query(User).options(joinedload(User.profile)) \\
                         .filter(User.id == int(user_id)).first()

    token_payload_builder
        Callable ``(user_id: int) -> dict`` that adds extra claims to every
        newly issued token (e.g. ``{"role": "admin"}``).

    db_dependency
        The project's ``get_db`` generator function.  When omitted, the
        library auto-discovers it via
        :func:`fastapi_mvt.utils.project.get_db_session`.  Explicit is
        better::

            from myproject.db import get_db
            AuthConfig(secret_key=..., db_dependency=get_db)
    """

    # ── Required ──────────────────────────────────────────────────────────────
    secret_key: str

    # ── User model ────────────────────────────────────────────────────────────
    user_model: str = "fastapi_mvt.auth.models.DefaultAuthUser"

    # ── JWT settings ──────────────────────────────────────────────────────────
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    enable_refresh_token: bool = True
    """
    Set to ``False`` to disable refresh tokens entirely.

    When disabled:

    * ``POST /auth/login`` and ``POST /auth/register`` return
      ``{"access_token": "...", "refresh_token": null, "token_type": "bearer"}``.
    * ``POST /auth/refresh`` returns **HTTP 404** so clients fail loudly
      instead of getting an unexpected 401.

    Example::

        configure_auth(AuthConfig(
            secret_key=settings.SECRET_KEY,
            enable_refresh_token=False,   # access-token-only mode
            db_dependency=get_db,
        ))
    """
    # Field inside the JWT payload that holds the user PK (default "sub").
    token_user_id_field: str = "sub"

    # ── Pluggable hooks ───────────────────────────────────────────────────────
    me_serializer:          Optional[Callable[[Any], Any]]       = field(default=None)
    current_user_loader:    Optional[Callable[[str, Any], Any]]  = field(default=None)
    token_payload_builder:  Optional[Callable[[int], dict]]      = field(default=None)

    # ── Database ──────────────────────────────────────────────────────────────
    db_dependency: Optional[Callable] = field(default=None)

    # ── Router extensions ─────────────────────────────────────────────────────
    extra_router: Optional[Any] = field(default=None)
    """
    An ``APIRouter`` whose routes are mounted under the ``/auth`` prefix.

    Use this to add project-specific endpoints alongside the built-in ones::

        from fastapi import APIRouter
        from myproject.db import get_db

        auth_extra = APIRouter()

        @auth_extra.get("/verify-email")
        def verify_email(token: str, db = Depends(get_db)):
            ...

        configure_auth(AuthConfig(
            secret_key=...,
            extra_router=auth_extra,   # available at GET /auth/verify-email
        ))
    """

    # ── Auth backend ──────────────────────────────────────────────────────────
    auth_backend: Optional[Any] = field(default=None)
    """
    Swap out the entire token / password mechanism.

    Provide an instance of any class that subclasses
    :class:`~fastapi_mvt.auth.backends.AuthBackend`.  When ``None`` (the
    default) the library uses :class:`~fastapi_mvt.auth.backends.JWTAuthBackend`
    built automatically from ``secret_key``, ``algorithm``, and the expire
    settings above.

    Example — replace JWT with opaque API keys::

        from fastapi_mvt.auth.backends import AuthBackend

        class APIKeyBackend(AuthBackend):
            _store: dict[str, int] = {}

            def create_access_token(self, user_id, extra=None):
                import secrets
                key = secrets.token_urlsafe(32)
                self._store[key] = user_id
                return key

            def create_refresh_token(self, user_id): return ""

            def decode_access_token(self, token):
                uid = self._store.get(token)
                if uid is None:
                    from fastapi import HTTPException, status
                    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid key")
                return {"sub": str(uid)}

            def decode_refresh_token(self, token):
                from fastapi import HTTPException, status
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "No refresh tokens")

            def revoke_token(self, token): self._store.pop(token, None)

            def hash_password(self, plain):
                from fastapi_mvt.auth._jwt import hash_password
                return hash_password(plain)

            def verify_password(self, plain, hashed):
                from fastapi_mvt.auth._jwt import verify_password
                return verify_password(plain, hashed)

        configure_auth(AuthConfig(
            secret_key="unused",
            auth_backend=APIKeyBackend(),
        ))
    """

    # ── Built-in endpoint overrides ───────────────────────────────────────────
    register_handler: Optional[Callable] = field(default=None)
    """
    Replace the built-in ``POST /auth/register`` handler.

    The callable receives the **same arguments** FastAPI would inject::

        from fastapi import Response
        from fastapi_mvt.auth import RegisterRequest, TokenResponse

        def my_register(
            payload: RegisterRequest,
            response: Response,
            db,           # injected by FastAPI via the db_dependency
        ) -> TokenResponse:
            # your custom registration logic
            ...

        configure_auth(AuthConfig(
            secret_key=...,
            register_handler=my_register,
        ))

    When set, the default duplicate-email check, password hashing, and token
    issuance are all skipped — the handler is fully responsible for the
    response.
    """


# ── Public API ────────────────────────────────────────────────────────────────

def configure_auth(config: AuthConfig) -> None:
    """
    Register *config* as the active auth configuration.

    Call this exactly once, typically in your app's startup or ``main.py``::

        configure_auth(AuthConfig(secret_key=os.environ["SECRET_KEY"]))

    If ``extra_router`` is provided it is wired into the auth router
    immediately, so it is visible when ``app.include_router(auth_router)``
    is called afterwards.
    """
    global _auth_config
    _auth_config = config

    # Wire extra routes into the shared auth router before the app mounts it.
    if config.extra_router is not None:
        from fastapi_mvt.auth.router import router as _auth_router  # local import avoids circular deps
        _auth_router.include_router(config.extra_router)


def get_backend():
    """
    Return the active :class:`~fastapi_mvt.auth.backends.AuthBackend`.

    If ``AuthConfig.auth_backend`` is set, returns it as-is.
    Otherwise builds a :class:`~fastapi_mvt.auth.backends.JWTAuthBackend`
    from the current config values.
    """
    cfg = get_auth_config()
    if cfg.auth_backend is not None:
        return cfg.auth_backend
    from fastapi_mvt.auth.backends import JWTAuthBackend
    return JWTAuthBackend(
        secret_key=cfg.secret_key,
        algorithm=cfg.algorithm,
        access_token_expire_minutes=cfg.access_token_expire_minutes,
        refresh_token_expire_days=cfg.refresh_token_expire_days,
    )


def get_auth_config() -> AuthConfig:
    """
    Return the active :class:`AuthConfig`.

    Raises :class:`RuntimeError` if :func:`configure_auth` has not been
    called yet — this surfaces as a 500 during app startup rather than a
    silent misconfiguration.
    """
    if _auth_config is None:
        raise RuntimeError(
            "fastapi-mvt auth is not configured. "
            "Call configure_auth(AuthConfig(secret_key=...)) "
            "before your application starts accepting requests."
        )
    return _auth_config


def get_user_model() -> Any:
    """
    Resolve and return the active user model class.

    Reads ``AuthConfig.user_model`` (a dotted import path), imports the
    module, and returns the class.  The result is *not* cached so that test
    isolation (swapping configs) works without side-effects.

    Example::

        UserModel = get_user_model()
        user = db.query(UserModel).filter(UserModel.id == 1).first()
    """
    config = get_auth_config()
    module_path, class_name = config.user_model.rsplit(".", 1)
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name, None)
    if cls is None:
        raise ImportError(
            f"Cannot find class '{class_name}' in module '{module_path}'. "
            f"Check the 'user_model' value in your AuthConfig."
        )
    return cls


__all__ = [
    "AuthConfig",
    "configure_auth",
    "get_auth_config",
    "get_user_model",
]
