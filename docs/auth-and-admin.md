# Authentication and admin

Both are optional extras: `pip install "fastapi-mvt[auth]"` and `"fastapi-mvt[admin]"`.
Projects created with `startproject` include auth.

## The user model

```python
from fastapi_mvt.auth import AbstractUser
from fastapi_mvt.db import CharField, TimestampedModel

class User(AbstractUser, TimestampedModel):
    full_name = CharField(max_length=150, null=True)
```

`AbstractUser` provides `email` (unique), `password` (hashed, private),
`is_active`, `is_superuser`, `last_login`, private `token_version`,
`failed_logins` and `locked_until` columns, and
`await user.set_password(raw)` / `await user.check_password(raw)`. The model is
yours: add fields, run `makemigrations`.

Passwords are hashed with PBKDF2-SHA256 in **Django's format**, so users imported
from a Django project keep their passwords. Hashing runs in a worker thread
instead of blocking the event loop. Hashes from fastapi-mvt 0.1 (bcrypt) still
verify if `bcrypt` is installed, and are upgraded at the next login.

## Setup

```python
# myproject/auth.py
from typing import Annotated
from fastapi import Depends
from fastapi_mvt.auth import Auth
from users.models import User

auth = Auth(User, secret_key=settings.secret_key)      # 32+ characters, required
CurrentUser = Annotated[User, Depends(auth.current_user)]
Superuser = Annotated[User, Depends(auth.superuser)]

# main.py
app.include_router(auth.router)
```

## Endpoints

| Endpoint | |
|---|---|
| `POST /auth/register` | `{email, password}` → the user (201). Duplicate email → 409 |
| `POST /auth/login` | `{email, password}` → `{access_token, refresh_token, token_type, expires_in}` |
| `POST /auth/token` | same, as an OAuth2 form, so the **Authorize** button in `/docs` works |
| `POST /auth/refresh` | `{refresh_token}` → new tokens |
| `POST /auth/logout` | revokes **all** of the user's tokens (every device) |
| `GET /auth/me` | the current user |
| `POST /auth/password` | `{current_password, new_password}`; revokes existing tokens |

Emails are stored lowercased. A wrong email and a wrong password give the same
error and take the same time, so attackers can't probe which emails exist.

## Login protection

After `max_failed_logins` wrong passwords in a row (default 5) the account is
locked for `lockout_minutes` (default 15): `/auth/login` answers **429** with a
`Retry-After` header, even for the right password. A successful login resets the
counter. The counter is stored on the user row, so it works across all server
processes. `max_failed_logins=None` turns it off.

Per-account lockout can be abused to lock someone out on purpose; for public
sites, also rate-limit by IP at your proxy (or with a library like `slowapi`).

## Password reset

Pass a function that delivers the reset token, usually by email, and two
endpoints appear:

```python
async def send_password_reset(user: User, token: str) -> None:
    await send_email(user.email, "Reset your password", f"https://app.example.com/reset?token={token}")

auth = Auth(User, secret_key=settings.secret_key, send_password_reset=send_password_reset)
```

| Endpoint | |
|---|---|
| `POST /auth/password-reset` | `{email}` → 202, always the same answer (doesn't reveal which emails exist); the token is sent in the background |
| `POST /auth/password-reset/confirm` | `{token, new_password}` → 204; the token works once, expires after 30 minutes (`password_reset_minutes`), and all existing logins are revoked |

## Protecting endpoints

```python
@router.get("/me/orders")
async def my_orders(user: CurrentUser):
    return await Order.objects.filter(user_id=user.id)

@router.delete("/users/{id}")
async def remove_user(id: int, admin: Superuser): ...
```

`auth.optional_user` returns `None` for anonymous requests. Role checks are
ordinary dependencies; see `require_role` in the [tutorial](tutorial.md#2-users-and-roles).

## How tokens are revoked

Access tokens are short-lived JWTs (30 minutes by default) carrying the user's
`token_version`. Logout and password changes increment it, which invalidates
every earlier token at once, on every server process, with no blacklist to keep
in sync. Inactive users (`is_active=False`) are rejected immediately.

## Customising

```python
class MyAuth(Auth):
    async def create_user(self, data):
        user = await super().create_user(data)
        on_commit(lambda: send_welcome_email.delay(user.id))
        return user

auth = MyAuth(
    User,
    secret_key=...,
    register_schema=SignUp,          # accept extra fields (subclass RegisterRequest)
    user_schema=UserRead,            # what /auth/me and /register return
    allow_registration=False,        # invite-only
    access_token_minutes=15,
    refresh_token_days=30,
)
```

Need OAuth/social login, API keys or sessions? `fastapi-mvt` models are plain
SQLAlchemy, so libraries like fastapi-users or Authlib work with them. Use
`db.sessionmaker` where a library asks for a session factory.

## Admin site

```bash
pip install "fastapi-mvt[admin]"
python manage.py createsuperuser
```

```python
from fastapi_mvt.admin import setup_admin

setup_admin(app, db, auth=auth)                          # all models at /admin
setup_admin(app, db, auth=auth, models=[User, Event])   # or pick some
```

Only active superusers can log in. `private()` columns (like password hashes)
are hidden from lists, detail pages and forms. The admin is
[SQLAdmin](https://aminalaee.dev/sqladmin/); for custom views, use its `ModelView`
API directly, since your models are ordinary SQLAlchemy models.
