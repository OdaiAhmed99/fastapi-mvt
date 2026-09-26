"""Authentication (``pip install "fastapi-mvt[auth]"``).

::

    # users/models.py
    class User(AbstractUser, TimestampedModel):
        full_name: Mapped[str | None] = mapped_column(String(150))

    # myproject/auth.py
    auth = Auth(User, secret_key=settings.secret_key)
    CurrentUser = Annotated[User, Depends(auth.current_user)]

    # myproject/main.py
    app.include_router(auth.router)   # /auth/register, /login, /token, /refresh, /logout, /me, /password

Access tokens are short-lived JWTs. Each user has a ``token_version``:
logging out or changing the password increments it, which invalidates every
token issued before, on all workers, without a token blacklist.
"""


import hashlib
import inspect
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Union

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from fastapi_mvt.auth.passwords import (
    acheck_password,
    amake_password,
    check_password,
    dummy_hash,
    make_password,
    needs_rehash,
)
from fastapi_mvt.db import commit_on_error, in_transaction, model_schema, private
from fastapi_mvt.db.models import utcnow

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class AbstractUser:
    """Columns and methods for a login-capable user. Combine with a model base::

    class User(AbstractUser, TimestampedModel): ...
    """

    email: Mapped[str] = mapped_column(String(254), unique=True, index=True, sort_order=-50)
    password: Mapped[str] = private(String(128), sort_order=-49)
    is_active: Mapped[bool] = mapped_column(default=True, sort_order=-48)
    is_superuser: Mapped[bool] = mapped_column(default=False, sort_order=-47)
    last_login: Mapped[Optional[datetime]] = mapped_column(default=None, sort_order=-46)
    token_version: Mapped[int] = private(default=0, server_default="0", sort_order=-45)
    failed_logins: Mapped[int] = private(default=0, server_default="0", sort_order=-44)
    locked_until: Mapped[Optional[datetime]] = private(default=None, sort_order=-43)

    async def set_password(self, raw_password: str) -> None:
        """Hash and store a password (call ``save()`` afterwards)."""
        self.password = await amake_password(raw_password)

    async def check_password(self, raw_password: str) -> bool:
        return await acheck_password(raw_password, self.password)

    def set_unusable_password(self) -> None:
        self.password = "!"


# ── Schemas ─────────────────────────────────────────────────────────────────


class LoginRequest(BaseModel):
    email: str = Field(max_length=254, pattern=EMAIL_PATTERN, examples=["ada@example.com"])
    password: str = Field(max_length=128)


class RegisterRequest(LoginRequest):
    password: str = Field(min_length=8, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class PasswordResetRequest(BaseModel):
    email: str = Field(max_length=254, pattern=EMAIL_PATTERN)


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Seconds until the access token expires")


_UNAUTHORIZED = {"WWW-Authenticate": "Bearer"}


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail=detail, headers=_UNAUTHORIZED)


# No `from __future__ import annotations` in this module: FastAPI must see the
# real schema classes in the route signatures built inside Auth.
class Auth:
    def __init__(
        self,
        user_model: Any,
        *,
        secret_key: str,
        access_token_minutes: int = 30,
        refresh_token_days: int = 7,
        algorithm: str = "HS256",
        prefix: str = "/auth",
        allow_registration: bool = True,
        register_schema: type[RegisterRequest] = RegisterRequest,
        user_schema: Optional[type[BaseModel]] = None,
        max_failed_logins: Optional[int] = 5,
        lockout_minutes: int = 15,
        send_password_reset: Optional[Callable[[Any, str], Union[Awaitable[None], None]]] = None,
        password_reset_minutes: int = 30,
    ) -> None:
        """
        ``max_failed_logins`` wrong passwords in a row lock the account for
        ``lockout_minutes`` (``None`` disables this). ``send_password_reset(user, token)``
        delivers reset tokens (usually by email); when given, ``/auth/password-reset``
        endpoints are added.
        """
        if not issubclass(user_model, AbstractUser):
            raise TypeError(f"{user_model.__name__} must inherit from fastapi_mvt.auth.AbstractUser")
        if len(secret_key) < 32:
            raise ValueError(
                "secret_key must be at least 32 characters. Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(50))"'
            )
        self.user_model = user_model
        self.secret_key = secret_key
        self.algorithm = algorithm
        self.access_lifetime = timedelta(minutes=access_token_minutes)
        self.refresh_lifetime = timedelta(days=refresh_token_days)
        self.prefix = prefix
        self.allow_registration = allow_registration
        self.register_schema = register_schema
        self.user_schema = user_schema or model_schema(user_model, "read", name="UserRead")
        self.max_failed_logins = max_failed_logins
        self.lockout = timedelta(minutes=lockout_minutes)
        self.send_password_reset = send_password_reset
        self.reset_lifetime = timedelta(minutes=password_reset_minutes)
        self.scheme = OAuth2PasswordBearer(tokenUrl=f"{prefix}/token", auto_error=False)
        self._router: Optional[APIRouter] = None

        scheme = self.scheme

        async def current_user(token: Optional[str] = Depends(scheme)) -> Any:
            """The logged-in user; 401 without a valid access token."""
            if not token:
                raise _unauthorized("Not authenticated")
            return await self.user_from_token(token, "access")

        async def optional_user(token: Optional[str] = Depends(scheme)) -> Any:
            """The logged-in user, or None for anonymous requests."""
            if not token:
                return None
            return await self.user_from_token(token, "access")

        async def superuser(user: Any = Depends(current_user)) -> Any:
            if not user.is_superuser:
                raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Admin rights required.")
            return user

        self.current_user = current_user
        self.optional_user = optional_user
        self.superuser = superuser

    # ── Tokens ──────────────────────────────────────────────────────────────

    def _jwt(self) -> Any:
        try:
            import jwt
        except ImportError:
            raise RuntimeError('PyJWT is not installed: pip install "fastapi-mvt[auth]"') from None
        return jwt

    def _encode(self, user: Any, kind: str, lifetime: timedelta) -> str:
        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(user.pk),
            "type": kind,
            "ver": user.token_version or 0,
            "iat": now,
            "exp": now + lifetime,
        }
        return self._jwt().encode(payload, self.secret_key, algorithm=self.algorithm)

    def create_access_token(self, user: Any) -> str:
        return self._encode(user, "access", self.access_lifetime)

    def create_refresh_token(self, user: Any) -> str:
        return self._encode(user, "refresh", self.refresh_lifetime)

    def create_tokens(self, user: Any) -> TokenPair:
        return TokenPair(
            access_token=self.create_access_token(user),
            refresh_token=self.create_refresh_token(user),
            expires_in=int(self.access_lifetime.total_seconds()),
        )

    def decode(self, token: str, kind: str) -> dict[str, Any]:
        jwt = self._jwt()
        try:
            payload = jwt.decode(
                token, self.secret_key, algorithms=[self.algorithm], options={"require": ["exp", "sub", "type"]}
            )
        except jwt.ExpiredSignatureError:
            raise _unauthorized("Token expired") from None
        except jwt.InvalidTokenError:
            raise _unauthorized("Invalid token") from None
        if payload.get("type") != kind:
            raise _unauthorized(f"Expected an {kind} token")
        return payload

    async def user_from_token(self, token: str, kind: str = "access") -> Any:
        payload = self.decode(token, kind)
        user = await self.user_model.objects.get_or_none(pk=self._parse_pk(payload["sub"]))
        if user is None or not user.is_active or (user.token_version or 0) != payload.get("ver", 0):
            raise _unauthorized("Invalid token")
        return user

    def _parse_pk(self, value: str) -> Any:
        from sqlalchemy import inspect

        mapper: Any = inspect(self.user_model)
        column = mapper.primary_key[0]
        try:
            return column.type.python_type(value)
        except (ValueError, TypeError, NotImplementedError):
            raise _unauthorized("Invalid token") from None

    # ── Users ───────────────────────────────────────────────────────────────

    async def authenticate(self, email: str, password: str) -> Any:
        """Return the active user with these credentials, or None.

        Raises 429 while an account is locked after too many failed attempts.
        """
        user = await self.user_model.objects.get_or_none(email=email.strip().lower())
        if user is None:
            await acheck_password(password, dummy_hash())
            return None
        now = utcnow()
        if self.max_failed_logins and user.locked_until and user.locked_until > now:
            minutes = max(1, math.ceil((user.locked_until - now).total_seconds() / 60))
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many failed logins. Try again in {minutes} minute(s).",
                headers={"Retry-After": str(minutes * 60)},
            )
        if not user.is_active or not await user.check_password(password):
            if self.max_failed_logins and user.is_active:
                await self._record_failed_login(user, now)
            return None
        if needs_rehash(user.password):
            await user.set_password(password)
        user.last_login = now
        user.failed_logins = 0
        user.locked_until = None
        await user.save()
        return user

    async def _record_failed_login(self, user: Any, now: datetime) -> None:
        # The login request ends in 401, which would roll the counter back.
        if in_transaction():
            try:
                commit_on_error()
            except Exception:
                pass
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= (self.max_failed_logins or 0):
            user.failed_logins = 0
            user.locked_until = now + self.lockout
        await user.save()

    async def create_user(self, data: BaseModel) -> Any:
        """Create a user from the registration payload. Override to customise sign-up."""
        fields = data.model_dump(exclude={"password"})
        fields["email"] = fields["email"].strip().lower()
        if await self.user_model.objects.filter(email=fields["email"]).exists():
            raise HTTPException(status.HTTP_409_CONFLICT, detail="A user with this email already exists.")
        user = self.user_model(**fields)
        await user.set_password(data.password)  # type: ignore[attr-defined]
        return await user.save()

    # ── Password reset ─────────────────────────────────────────────────────

    @staticmethod
    def _password_fingerprint(user: Any) -> str:
        # Changes whenever the password changes, so a reset token works only once.
        return hashlib.sha256((user.password or "").encode()).hexdigest()[:16]

    def create_password_reset_token(self, user: Any) -> str:
        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(user.pk),
            "type": "reset",
            "ver": user.token_version or 0,
            "pwd": self._password_fingerprint(user),
            "iat": now,
            "exp": now + self.reset_lifetime,
        }
        return self._jwt().encode(payload, self.secret_key, algorithm=self.algorithm)

    async def reset_password(self, token: str, new_password: str) -> Any:
        """Set a new password from a reset token; revokes all existing tokens."""
        try:
            payload = self.decode(token, "reset")
            user = await self.user_model.objects.get_or_none(pk=self._parse_pk(payload["sub"]))
        except HTTPException:
            user = None
        if (
            user is None
            or not user.is_active
            or payload.get("pwd") != self._password_fingerprint(user)
            or (user.token_version or 0) != payload.get("ver", 0)
        ):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="This reset link is invalid or has expired.")
        await user.set_password(new_password)
        user.failed_logins = 0
        user.locked_until = None
        await self.revoke_tokens(user)
        return user

    async def revoke_tokens(self, user: Any) -> None:
        """Invalidate every token issued to *user* so far."""
        user.token_version = (user.token_version or 0) + 1
        await user.save()

    # ── Router ──────────────────────────────────────────────────────────────

    @property
    def router(self) -> APIRouter:
        if self._router is None:
            self._router = self._build_router()
        return self._router

    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix=self.prefix, tags=["auth"])
        UserRead = self.user_schema
        RegisterSchema = self.register_schema
        current_user = self.current_user

        async def _login(email: str, password: str) -> TokenPair:
            user = await self.authenticate(email, password)
            if user is None:
                raise _unauthorized("Incorrect email or password")
            return self.create_tokens(user)

        if self.allow_registration:

            @router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
            async def register(data: RegisterSchema) -> Any:  # type: ignore[valid-type]
                """Create an account."""
                return await self.create_user(data)

        @router.post("/login", response_model=TokenPair)
        async def login(data: LoginRequest) -> TokenPair:
            """Exchange email and password for an access and a refresh token."""
            return await _login(data.email, data.password)

        @router.post("/token", response_model=TokenPair, include_in_schema=True)
        async def token(form: OAuth2PasswordRequestForm = Depends()) -> TokenPair:
            """OAuth2 password flow (used by the Authorize button in /docs). `username` is the email."""
            return await _login(form.username, form.password)

        @router.post("/refresh", response_model=TokenPair)
        async def refresh(data: RefreshRequest) -> TokenPair:
            """Exchange a refresh token for new tokens."""
            user = await self.user_from_token(data.refresh_token, "refresh")
            return self.create_tokens(user)

        @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
        async def logout(user: Any = Depends(current_user)) -> None:
            """Log out everywhere: every token issued so far stops working."""
            await self.revoke_tokens(user)

        @router.get("/me", response_model=UserRead)
        async def me(user: Any = Depends(current_user)) -> Any:
            return user

        if self.send_password_reset is not None:
            sender = self.send_password_reset

            @router.post("/password-reset", status_code=status.HTTP_202_ACCEPTED)
            async def request_password_reset(data: PasswordResetRequest, background: BackgroundTasks) -> dict:
                """Send a reset token to the email, if it belongs to an active account."""
                user = await self.user_model.objects.get_or_none(email=data.email.strip().lower())
                if user is not None and user.is_active:
                    token = self.create_password_reset_token(user)

                    async def deliver() -> None:
                        result = sender(user, token)
                        if inspect.isawaitable(result):
                            await result

                    # After the response, so response time doesn't reveal whether the email exists.
                    background.add_task(deliver)
                return {"detail": "If that email belongs to an account, a reset link is on its way."}

            @router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
            async def confirm_password_reset(data: PasswordResetConfirm) -> None:
                """Choose a new password with the token from the reset email."""
                await self.reset_password(data.token, data.new_password)

        @router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
        async def change_password(data: ChangePasswordRequest, user: Any = Depends(current_user)) -> None:
            """Change the password. Existing tokens are revoked."""
            if not await user.check_password(data.current_password):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="The current password is incorrect.")
            await user.set_password(data.new_password)
            await self.revoke_tokens(user)

        return router


__all__ = [
    "AbstractUser",
    "Auth",
    "ChangePasswordRequest",
    "PasswordResetConfirm",
    "PasswordResetRequest",
    "LoginRequest",
    "RefreshRequest",
    "RegisterRequest",
    "TokenPair",
    "check_password",
    "make_password",
]
