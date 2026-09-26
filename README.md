# fastapi-mvt

**Django's productivity for FastAPI, without leaving FastAPI or SQLAlchemy.**

fastapi-mvt is a library, not a framework around FastAPI. Your app stays a
normal FastAPI app; fastapi-mvt gives it:

- a **Django-style ORM layer on SQLAlchemy 2**: `Post.objects.filter(author__name="Ada")`,
  async all the way, no session plumbing;
- **Django's migration workflow on Alembic** (`makemigrations`, `migrate`,
  `showmigrations`), with the safety checks Alembic leaves out: rename
  detection, confirmation before data loss, one-off defaults for new required fields;
- **one transaction per request**, committed before the response is sent,
  plus `atomic()` and `on_commit()`;
- a **test harness**: isolated test database built from your migrations, each
  test rolled back;
- a **CLI**: `startproject`, `startapp --crud`, `createsuperuser`, `shell`, `check`;
- optional **auth** (JWT, Django-compatible password hashes), **admin**
  (SQLAdmin) and **WebSocket** fan-out.

```python
class Event(TimestampedModel):
    title = CharField(max_length=200)
    starts_at = DateTimeField()
    instructor_id = ForeignKey(User, on_delete=CASCADE)
    instructor: Mapped[User] = relationship()


class EventRead(ModelSchema, model=Event):          # API schema generated from the model
    pass


@router.get("/events", response_model=Page[EventRead])
async def list_events(params: PageParams = Depends(), q: str | None = None):
    events = Event.objects.filter(starts_at__gte=utcnow()).order_by("starts_at")
    if q:
        events = events.filter(Q(title__icontains=q) | Q(instructor__full_name__icontains=q))
    return await events.prefetch("instructor").paginate(params)


@router.post("/events/{event_id}/registrations", status_code=201)
async def register(event_id: int, student: Student):
    event = await Event.objects.get(id=event_id)                # 404 if missing
    registration = await Registration.objects.create(event_id=event.id, student_id=student.id)
    on_commit(lambda: send_confirmation.delay(registration.id))  # only if the request commits
    return registration                                          # duplicate -> 409 (unique constraint)
```

Every model is a real SQLAlchemy model and every QuerySet a real `select()`
(`qs.statement`), so everything SQLAlchemy can do is still one line away.
Generated projects pass ruff and mypy out of the box.

**Is it for you?** If you want Django itself, use Django with
[Django Ninja](https://django-ninja.dev/). fastapi-mvt is for teams on FastAPI
and SQLAlchemy who want Django's conveniences there. See the
[comparison](docs/comparison.md).

## Quick start

```bash
pip install "fastapi-mvt[auth,server,test]"
fastapi-mvt startproject shop
cd shop
pip install -e ".[test]"
python manage.py startapp catalog --crud Product    # model + schemas + CRUD router + tests
python manage.py makemigrations
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver                          # http://127.0.0.1:8000/docs
pytest
```

## Documentation

| | |
|---|---|
| [**Tutorial**](docs/tutorial.md) | Build an eLearning API step by step, from an empty folder to production |
| [ORM guide](docs/orm.md) | Models, relationships, queries, transactions, schemas, pagination |
| [Migrations guide](docs/migrations.md) | Commands, safety checks, teams, production |
| [Testing guide](docs/testing.md) | `client` / `db` fixtures, test databases |
| [Auth and admin](docs/auth-and-admin.md) | Users, tokens, permissions, the admin site |
| [Deployment](docs/deployment.md) | Settings, release steps, Docker, background jobs |
| [Command line](docs/cli.md) | Every command, and writing your own |
| [Comparison](docs/comparison.md) | vs Django Ninja, SQLModel, the full-stack template, plain SQLAlchemy |
| [Design notes](docs/design.md) | Why it's built this way |
| [Upgrading from 0.1](docs/upgrading-from-0.1.md) | |
| [Roadmap](ROADMAP.md) | Where it stands and what comes next |

The [showcase repository](https://github.com/OdaiAhmed99/fastapi-mvt-showcase)
is the tutorial's finished app.

## Commands

The full documentation is at **<https://odaiahmed99.github.io/fastapi-mvt/>** (built from `docs/`).

| Command | |
|---|---|
| `startproject <name>` / `startapp <name> [--crud Model]` | scaffold (never overwrites files) |
| `makemigrations` / `migrate [target]` / `showmigrations` / `rollback` / `sqlmigrate` | migrations |
| `runserver` / `shell` / `routes` / `createsuperuser` | development |
| `check` | CI: config, models, pending or missing migrations |
| your own | any app's `commands.py` adds `manage.py` commands ([CLI](docs/cli.md)) |

## Installation extras

| Extra | Adds |
|---|---|
| (core) | FastAPI, SQLAlchemy 2 (async), Alembic, pydantic-settings, SQLite driver |
| `postgres` | asyncpg |
| `mysql` | aiomysql (experimental: not covered by the test suite yet) |
| `auth` | PyJWT, python-multipart |
| `admin` | SQLAdmin |
| `redis` | multi-process WebSockets |
| `server` | uvicorn |
| `test` | pytest, pytest-asyncio, httpx |

Python 3.10+. Tested on SQLite and PostgreSQL 16.

## Development

```bash
pip install -e ".[dev]" --config-settings editable_mode=compat   # compat: so mypy can see the package
pytest                                   # SQLite
TEST_DATABASE_URL=postgresql://user:pass@localhost/mvt pytest   # PostgreSQL
pytest -m "not slow"                     # skip the end-to-end CLI tests
```

MIT licensed.
