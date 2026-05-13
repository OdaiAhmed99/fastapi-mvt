# FastAPI MVT

> A batteries-included project framework for FastAPI — structured, fast to start, and built for developers who want Django's ergonomics with FastAPI's performance.

[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104%2B-009688)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-gitbook-blue)](https://odaithalji.gitbook.io/fastapi)

---

## Overview

**FastAPI MVT** is a project scaffolding framework built on top of FastAPI.

It gives you a full project skeleton the moment you run one command — with authentication, database, WebSocket, signals, middleware, and CLI management all wired together and ready to use.

You write your business logic. The framework handles the plumbing.

---

## Features

| Feature | Description |
|---|---|
| **MVT Pattern** | Model → View → Template architecture familiar to Django developers |
| **JWT Authentication** | Built-in `/auth` router with register, login, logout, refresh, and `/me` endpoints |
| **SQLAlchemy + Alembic** | Async-ready ORM with full migration support via `makemigrations` / `migrate` |
| **Signals** | Django-style ORM signals (`pre_save`, `post_save`, `pre_delete`, `post_delete`) wired automatically via SQLAlchemy events |
| **WebSocket Manager** | Group-aware connection manager with optional Redis channel backend for multi-process scaling |
| **Security Middleware** | CORS, security headers, rate limiting, and SQL injection protection out of the box |
| **App Registry** | Auto-discovery system for routers, models, and signals across multiple apps |
| **CLI Management** | `startproject`, `startapp`, `makemigrations`, `migrate`, `checkmigrations` commands |

---

## Tech Stack

- **[FastAPI](https://fastapi.tiangolo.com/)** — high-performance async web framework
- **[SQLAlchemy 2.0](https://docs.sqlalchemy.org/)** — modern ORM with async support
- **[Alembic](https://alembic.sqlalchemy.org/)** — database schema migration tool
- **[Pydantic v2](https://docs.pydantic.dev/)** — data validation and settings management
- **[python-jose](https://github.com/mpdavis/python-jose)** — JWT token signing and verification
- **[Typer](https://typer.tiangolo.com/)** — CLI built on Click
- **[Jinja2](https://jinja.palletsprojects.com/)** — templating engine
- **[Celery](https://docs.celeryq.dev/) + [Redis](https://redis.io/)** — background task queue
- **[websockets](https://websockets.readthedocs.io/)** — WebSocket support with Redis pub/sub scaling

---

## Installation

```bash
pip install fastapi-mvt
```

Or install from source:

```bash
git clone https://github.com/yourusername/fastapi-mvt
cd fastapi-mvt
pip install -e .
```

---

## Quick Start

### 1. Create a new project

```bash
fastapi-mvt startproject myproject
cd myproject
```

### 2. Create an app

```bash
fastapi-mvt startapp blog
```

### 3. Run migrations

```bash
fastapi-mvt makemigrations
fastapi-mvt migrate
```

### 4. Start the server

```bash
uvicorn myproject.asgi:app --reload
```

---

## Project Structure

After running `startproject`, you get:

```
myproject/
├── manage.py               # CLI entry point
├── .env                    # Environment variables
├── myproject/
│   ├── settings.py         # Project settings (INSTALLED_APPS, DB, etc.)
│   ├── urls.py             # Root router
│   └── asgi.py             # ASGI application entry point
├── apps/
│   └── blog/               # An example app (via startapp)
│       ├── models.py       # SQLAlchemy models
│       ├── views.py        # FastAPI route handlers
│       ├── schemas.py      # Pydantic schemas
│       ├── signals.py      # Signal receivers
│       └── router.py       # APIRouter definition
└── migrations/
    └── versions/           # Alembic migration files
```

---

## Core Concepts

### Authentication

FastAPI MVT ships a plug-and-play JWT auth system. Configure it once in `settings.py`:

```python
from fastapi_mvt.auth import configure_auth, AuthConfig

configure_auth(AuthConfig(
    secret_key="your-secret-key",
    user_model="apps.accounts.models.User",   # omit to use built-in DefaultAuthUser
))
```

Then include the built-in auth router:

```python
from fastapi import FastAPI
from fastapi_mvt.auth import auth_router

app = FastAPI()
app.include_router(auth_router)
```

Available endpoints:

| Method | Path | Description |
|---|---|---|
| `POST` | `/auth/register` | Create account, returns token pair |
| `POST` | `/auth/login` | Authenticate, returns token pair |
| `POST` | `/auth/logout` | Revoke access token |
| `POST` | `/auth/refresh` | Exchange refresh token for new access token |
| `GET` | `/auth/me` | Return authenticated user identity |

Protect your own routes with the `get_current_user` dependency:

```python
from fastapi import Depends
from fastapi_mvt.auth import get_current_user

@router.get("/profile")
def profile(user=Depends(get_current_user)):
    return {"id": user.id, "email": user.email}
```

---

### Signals

Signals fire automatically when SQLAlchemy models are saved or deleted — no manual `.send()` calls needed inside views.

```python
from fastapi_mvt.core.signals import receiver, post_save
from apps.blog.models import Post

@receiver(post_save, sender=Post)
async def on_post_saved(sender, instance, created, **kwargs):
    if created:
        print(f"New post published: {instance.title}")
```

Register signals during startup:

```python
from fastapi_mvt.core.signals import register_all_model_signals
from myproject.db import Base

register_all_model_signals(Base)
```

Available signals: `pre_save`, `post_save`, `pre_delete`, `post_delete`, `post_init`

---

### WebSocket

A group-aware WebSocket connection manager is included. For multi-process deployments, swap in the Redis backend:

```python
from fastapi_mvt.utils.websocket import ConnectionManager, RedisChannelBackend
import redis.asyncio as aioredis

redis_client = aioredis.from_url("redis://localhost")
manager = ConnectionManager(channel_backend=RedisChannelBackend(redis_client))
```

Use in route handlers:

```python
from fastapi import WebSocket
from fastapi_mvt.utils.websocket import manager

@app.websocket("/ws/{room}")
async def websocket_endpoint(websocket: WebSocket, room: str):
    conn_id = await manager.connect(websocket, group=room)
    try:
        while True:
            data = await websocket.receive_text()
            await manager.send_to_group(room, {"message": data})
    finally:
        await manager.disconnect(conn_id)
```

---

### Middleware

Security middleware is applied automatically. You can also configure it manually:

```python
from fastapi_mvt.middleware import setup_security_middleware, setup_cors_middleware

setup_cors_middleware(app, origins=["https://example.com"])
setup_security_middleware(app)
```

Built-in middleware:

- `SecurityHeadersMiddleware` — adds `X-Content-Type-Options`, `X-Frame-Options`, `HSTS`, `CSP`, and more
- `SQLInjectionProtectionMiddleware` — blocks common SQL injection patterns in request parameters
- `RateLimitMiddleware` — request-rate throttling per IP
- `CustomMiddleware` — base class for your own middleware

---

### App Registry

Register apps in `settings.py` and the framework auto-discovers their routers, models, and signals:

```python
INSTALLED_APPS = [
    "apps.blog",
    "apps.accounts",
]
```

---

## CLI Reference

| Command | Description |
|---|---|
| `fastapi-mvt startproject <name>` | Scaffold a new project |
| `fastapi-mvt startapp <name>` | Scaffold a new app inside a project |
| `fastapi-mvt makemigrations [app]` | Generate Alembic migration from model changes |
| `fastapi-mvt migrate` | Apply pending migrations to the database |
| `fastapi-mvt checkmigrations` | Report empty or no-op migration files |

---

## Configuration

All settings live in your project's `settings.py`. Key options:

```python
# Database
DATABASE_URL = "sqlite+aiosqlite:///./db.sqlite3"
# DATABASE_URL = "postgresql+asyncpg://user:pass@localhost/dbname"

# Auth
SECRET_KEY = "change-me-in-production"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 7

# Apps
INSTALLED_APPS = [
    "apps.blog",
    "apps.accounts",
]

# CORS
CORS_ORIGINS = ["http://localhost:3000"]
```

---

## Documentation

Full documentation is available at **[https://odaithalji.gitbook.io/fastapi](https://odaithalji.gitbook.io/fastapi)**

---

## License

MIT License — see [LICENSE](LICENSE) for details.
