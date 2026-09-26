# Deployment

## Settings

All settings come from environment variables (or `.env` locally):

| Variable | Notes |
|---|---|
| `SECRET_KEY` | **Required.** 32+ random characters: `python -c "import secrets; print(secrets.token_urlsafe(50))"` |
| `DATABASE_URL` | `postgresql://user:pass@host:5432/db`. The async driver is chosen automatically |
| `DEBUG` | `false` in production |
| `ALLOWED_ORIGINS` | JSON list of browser origins for CORS, e.g. `["https://app.example.com"]` |
| `ACCESS_TOKEN_MINUTES`, `REFRESH_TOKEN_DAYS` | token lifetimes |

Install the PostgreSQL driver: `pip install "fastapi-mvt[postgres]"`.

## Release steps

```bash
python manage.py check          # models, app import, migrations
python manage.py migrate        # before the new code starts serving
uvicorn myproject.main:app --host 0.0.0.0 --port 8000 --workers 4 --proxy-headers
```

Run `migrate` once per release (a release job, init container or deploy hook),
not in every web process.

## Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY . .
RUN pip install --no-cache-dir ".[postgres]"
CMD ["uvicorn", "myproject.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--proxy-headers"]
```

Release command: `python manage.py migrate`.

## Connection pool

`Database(url, pool_size=10, max_overflow=20, pool_pre_ping=True)` passes options
to SQLAlchemy's engine. Budget `workers × (pool_size + max_overflow)` connections
against your PostgreSQL `max_connections`.

## Background jobs

Enqueue jobs in `on_commit` so they never see uncommitted data:

```python
on_commit(lambda: send_receipt.delay(order.id))
```

Inside a Celery (or RQ, Dramatiq, ...) worker, run ORM code with `db.run(async_function, *args)`.

## WebSockets across processes

Use `ConnectionManager(RedisChannelBackend(redis.asyncio.from_url(...)))`
(`pip install "fastapi-mvt[redis]"`) and start/stop it in the app lifespan:

```python
@asynccontextmanager
async def lifespan(app):
    await manager.start()
    yield
    await manager.stop()
```

## Security checklist

- `DEBUG=false`, a strong `SECRET_KEY`, HTTPS in front of the app.
- `ALLOWED_ORIGINS` lists your front-ends; there is no `*` default.
- Rate limiting belongs in your proxy or in a library like `slowapi`; fastapi-mvt
  doesn't pretend to do it in-process.
