# Tutorial: build an eLearning API

In this tutorial you build a real API from an empty folder: students and
instructors, events, registrations and live "like" counts. Along the way you
use every everyday feature of fastapi-mvt:

| Part | You learn |
|---|---|
| 1. Create the project | `startproject`, settings, the first migration |
| 2. Users and roles | extending the user model, enum fields, sign-up |
| 3. Your first app | `startapp`, models, relationships, foreign keys |
| 4. Migrations | `makemigrations`, `migrate`, going backwards |
| 5. Schemas | `ModelSchema`, validation |
| 6. Endpoints | queries, pagination, 404/409 handling |
| 7. Business rules | a service layer, permissions, transactions |
| 8. Real-time likes | `on_commit`, WebSockets |
| 9. Tests | the `client` / `db` fixtures |
| 10. Changing the schema safely | renames, NOT NULL columns, deletions |
| 11. Admin and production | admin site, PostgreSQL, deployment |

The finished code is the [fastapi-mvt-showcase](https://github.com/OdaiAhmed99/fastapi-mvt-showcase)
repository. Every snippet below comes from it and is covered by its tests.

**You need** Python 3.10+ and basic FastAPI knowledge. Knowing Django helps (the
commands are the same) but is not required. You do **not** need to know SQLAlchemy.

---

## The finished project

This is the complete eLearning project you'll have at the end: about 580 lines
of your own Python code. Files marked *(generated)* come from `startproject` or
`startapp` and are left as they are; the rest you write in the part shown.

```text
elearning/                          ← project root (run every command from here)
│
├── manage.py                       (generated) `python manage.py <command>`
├── pyproject.toml                  (generated) dependencies + [tool.fastapi-mvt] config
├── .env                            (generated) local settings + SECRET_KEY — never committed
├── .env.example                    (generated) the settings template you do commit
├── .gitignore                      (generated)
├── README.md
│
├── elearning/                      ← project package: wiring only, no business logic
│   ├── __init__.py
│   ├── settings.py                 (generated) typed settings from env vars / .env
│   ├── db.py                       (generated) db = Database(settings.database_url)
│   ├── main.py                     (generated) FastAPI app, db.init_app(app), routers  · part 1, 3
│   ├── auth.py                     Auth setup, CurrentUser / Instructor / Student types · part 2
│   └── websocket.py                the shared ConnectionManager                        · part 8
│
├── users/                          ← app: accounts
│   ├── __init__.py
│   ├── models.py                   User(AbstractUser) + Role enum                      · part 2
│   └── schemas.py                  SignUp (register payload), UserSummary              · part 2
│
├── events/                         ← app: the eLearning domain
│   ├── __init__.py
│   ├── models.py                   Event, Registration, Like                           · part 3
│   ├── schemas.py                  EventCreate/Update/Read, Attendee, Like*            · part 5
│   ├── services.py                 business rules: ownership, registration, likes      · part 7, 8
│   └── router.py                   HTTP + WebSocket endpoints under /events            · part 6, 7, 8
│
├── migrations/                     ← created by the first `makemigrations`
│   ├── env.py                      (generated) 2 lines, calls fastapi-mvt — don't edit
│   ├── script.py.mako              (generated) template for new migration files
│   ├── README                      (generated)
│   └── versions/                   your schema history — commit these files
│       ├── 0001_initial.py                     users table                             · part 1
│       ├── 0002_users_role.py                  users.role column                       · part 2
│       └── 0003_create_events_and_more.py      events, registrations, likes            · part 4
│
└── tests/
    ├── test_api.py                 (generated) health check + sign-up/login flow
    └── test_events.py              permissions, validation, registrations, likes      · part 9
```

> The showcase repository was built in one go, so its `migrations/versions/` holds
> a single `0001_initial.py` with all four tables. Following this tutorial step by
> step gives you the three migrations shown above. Both are correct: migrations
> record *how* you got to a schema.

### How the pieces fit together

**An app is a package with a `models.py`.** `users/` and `events/` are apps.
Every package with a `models.py` is picked up automatically by migrations and
tests (`models = "auto"` in `pyproject.toml`). Routers are included explicitly in
`main.py`, so you can always see which endpoints exist.

**Each file in an app has one job:**

| File | Contains | Talks to |
|---|---|---|
| `models.py` | tables: fields, foreign keys, relationships | the database (via the ORM) |
| `schemas.py` | what the API accepts and returns | nothing (pure validation) |
| `services.py` | rules: who may do what, what must be true | models |
| `router.py` | endpoints: parse the request, call a service, return a schema | services, schemas |

Small apps can skip `services.py` and query models straight from the router, as
the `startapp --crud` template does. Add it once rules appear.

**What happens on `POST /events/7/registrations`:**

```text
request
  → elearning/main.py        db.init_app: open a transaction for this request
  → events/router.py         register(event_id=7, student: Student)
      → elearning/auth.py    Student dependency: valid token? role == student? (401 / 403)
  → events/services.py       register(): event exists? (404) not your own event? (400)
      → events/models.py     Registration.objects.create(...)  — duplicate? (409, unique constraint)
  → events/schemas.py        RegistrationRead shapes the JSON response
  → elearning/main.py        commit, then send 201 — or roll back everything on any error
```

Now let's build it.

---

## 1. Create the project

```bash
pip install "fastapi-mvt[auth,server,test]"
fastapi-mvt startproject elearning
cd elearning
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[test]"
```

You now have:

```
elearning/
├── manage.py              # python manage.py <command>, like Django
├── pyproject.toml         # dependencies + [tool.fastapi-mvt] config
├── .env                   # local settings, with a generated SECRET_KEY (git-ignored)
├── .env.example
├── elearning/
│   ├── settings.py        # typed settings from env vars / .env
│   ├── db.py              # db = Database(settings.database_url)
│   ├── auth.py            # JWT auth + the CurrentUser type
│   └── main.py            # the FastAPI app
├── users/
│   └── models.py          # your User model
└── tests/
    └── test_api.py
```

Open `elearning/main.py`. It is ordinary FastAPI with one extra line:

```python
app = FastAPI(title="elearning", debug=settings.debug)

# One transaction per request, ORM errors mapped to 404 / 409.
db.init_app(app)
```

That line gives every request its own database transaction. It commits when
your endpoint returns successfully and rolls back if the endpoint raises. More on
that in part 7.

Create the database tables and start the server:

```bash
python manage.py makemigrations     # writes migrations/versions/0001_initial.py
python manage.py migrate
python manage.py runserver
```

Open <http://127.0.0.1:8000/docs>. Sign-up, login, refresh, logout and `/auth/me`
already work. Click **Authorize** to log in from the docs page.

> **Where is the database?** By default it's a SQLite file, `db.sqlite3`, in the
> project folder. Set `DATABASE_URL=postgresql://user:pass@host/dbname` in `.env`
> to use PostgreSQL; nothing else changes (see part 11).

---

## 2. Users and roles

Our platform has students and instructors. Open `users/models.py` and add a role:

```python
"""Users of the platform: students, instructors and admins."""

import enum

from fastapi_mvt.auth import AbstractUser
from fastapi_mvt.db import CharField, EnumField, TimestampedModel


class Role(str, enum.Enum):
    STUDENT = "student"
    INSTRUCTOR = "instructor"
    ADMIN = "admin"


class User(AbstractUser, TimestampedModel):
    # Provided by AbstractUser: email, password (hashed), is_active, is_superuser, last_login.
    full_name = CharField(max_length=150, null=True)
    role = EnumField(Role, default=Role.STUDENT)
```

Fields read like Django's:

* `CharField(max_length=150, null=True)` is a VARCHAR(150) that may be empty.
  Fields are required (NOT NULL) unless you pass `null=True`, as in Django.
* `EnumField(Role, default=Role.STUDENT)` stores the enum's value (`"student"`)
  in a text column, like Django `choices`.
* Others: `TextField`, `IntegerField`, `DecimalField`, `BooleanField`,
  `DateTimeField(auto_now_add=True)`, `DateField`, `EmailField`, `SlugField`,
  `UUIDField`, `JSONField`, `ForeignKey` (see the [ORM guide](orm.md#defining-models)).
* `TimestampedModel` adds `id`, `created_at` and `updated_at`. Use `Model` for
  just an `id`.

Under the hood each field is a normal SQLAlchemy column, and your editor knows
`user.full_name` is `str | None`. If you prefer SQLAlchemy's own spelling,
`full_name: Mapped[Optional[str]] = mapped_column(String(150))` means exactly
the same, and you can mix both styles.

### Sign-up with a role

The built-in `/auth/register` accepts `email` and `password`. To accept more
fields, give it your own schema. Create `users/schemas.py`:

```python
from typing import Optional

from pydantic import Field

from fastapi_mvt.auth import RegisterRequest
from fastapi_mvt.db import Schema
from users.models import Role


class SignUp(RegisterRequest):
    """Registration payload: the default email/password plus our own fields."""

    full_name: Optional[str] = Field(default=None, max_length=150)
    # Admins are created with `manage.py createsuperuser`, never through sign-up.
    role: Role = Field(default=Role.STUDENT, description="student or instructor")


class UserSummary(Schema):
    id: int
    email: str
    full_name: Optional[str]
```

Then replace `elearning/auth.py`. We customise sign-up by overriding one method
and add role-based permissions as reusable types:

```python
from typing import Annotated

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel

from elearning.settings import settings
from fastapi_mvt.auth import Auth
from users.models import Role, User
from users.schemas import SignUp


class ElearningAuth(Auth):
    async def create_user(self, data: BaseModel) -> User:
        if getattr(data, "role", Role.STUDENT) is Role.ADMIN:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Admins cannot sign up.")
        return await super().create_user(data)


auth = ElearningAuth(
    User,
    secret_key=settings.secret_key,
    access_token_minutes=settings.access_token_minutes,
    refresh_token_days=settings.refresh_token_days,
    register_schema=SignUp,
)

CurrentUser = Annotated[User, Depends(auth.current_user)]


def require_role(role: Role):
    async def dependency(user: CurrentUser) -> User:
        if user.role is not role and not user.is_superuser:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=f"Only {role.value}s can do this.")
        return user

    return dependency


Instructor = Annotated[User, Depends(require_role(Role.INSTRUCTOR))]
Student = Annotated[User, Depends(require_role(Role.STUDENT))]
```

Any endpoint can now say `user: Instructor`, and FastAPI returns 401 for
anonymous users and 403 for students. That is plain FastAPI dependency
injection; fastapi-mvt adds no second system.

The model changed, so record the change:

```bash
python manage.py makemigrations
```
```
Migrations:
  migrations/versions/0002_users_role.py
    - Add column role to users
```

```bash
python manage.py migrate
```

---

## 3. Your first app: events

```bash
python manage.py startapp events
```

This creates `events/` (models, schemas, router) and a test file, and registers
the router in `elearning/main.py` for you. Nothing is discovered by magic at
runtime: open `main.py` and you'll see the two lines it added.

Write the models in `events/models.py`:

```python
"""Events that instructors publish, and students register for and like."""

from fastapi_mvt.db import (
    CASCADE,
    CharField,
    DateTimeField,
    ForeignKey,
    Mapped,
    TextField,
    TimestampedModel,
    relationship,
)
from sqlalchemy import UniqueConstraint

from users.models import User


class Event(TimestampedModel):
    title = CharField(max_length=200)
    description = TextField(null=True)
    location = CharField(max_length=255, null=True)
    starts_at = DateTimeField()
    ends_at = DateTimeField()
    instructor_id = ForeignKey(User, on_delete=CASCADE)

    instructor: Mapped[User] = relationship()
    registrations: Mapped[list["Registration"]] = relationship(back_populates="event")
    likes: Mapped[list["Like"]] = relationship(back_populates="event")


class Registration(TimestampedModel):
    # The database guarantees one registration per student and event, even under
    # concurrent requests; a duplicate becomes a 409 automatically.
    __table_args__ = (UniqueConstraint("event_id", "student_id"),)

    event_id = ForeignKey(Event, on_delete=CASCADE)
    student_id = ForeignKey(User, on_delete=CASCADE)

    event: Mapped[Event] = relationship(back_populates="registrations")
    student: Mapped[User] = relationship()


class Like(TimestampedModel):
    __table_args__ = (UniqueConstraint("event_id", "user_id"),)

    event_id = ForeignKey(Event, on_delete=CASCADE)
    user_id = ForeignKey(User, on_delete=CASCADE)
    reaction = CharField(max_length=30, default="like")

    event: Mapped[Event] = relationship(back_populates="likes")
```

What's new:

* **`ForeignKey(Model, on_delete=...)`** declares the foreign-key column
  (`instructor_id`). `on_delete` is required, as in Django: `CASCADE`, `SET_NULL`, `PROTECT`/`RESTRICT`. The *database*
  enforces it (SQLite included), so deleting an event deletes its registrations
  even when you delete in bulk.
* **`relationship()`** gives Python access to related rows:
  `registration.event`, `event.registrations`. Relationships are **never loaded
  behind your back**. You ask for them in the query (`.prefetch("student")`)
  or on an object (`await event.load("registrations")`). Forgetting raises a
  clear error naming the attribute instead of silently running one query per
  row (the classic N+1 problem).
* Table names come from the class: `Event` → `events`,
  `Registration` → `registrations`. Set `__tablename__` to choose your own.

---

## 4. Migrations

```bash
python manage.py makemigrations
```
```
Migrations:
  migrations/versions/0003_create_events_and_more.py
    - Create table events
    - Create index ix_events_instructor_id on events
    - Create table likes
    ...
```

```bash
python manage.py migrate
```
```
Running migrations:
  Applying 0003_create_events_and_more... OK
```

Migration files are plain Alembic scripts in `migrations/versions/`. Read them,
commit them, edit them if you need to. The commands you'll use:

```bash
python manage.py showmigrations      # [X] applied, [ ] pending
python manage.py migrate 0002        # go back to 0002 (unapplies 0003)
python manage.py migrate             # forward to the latest again
python manage.py rollback            # undo the last migration
python manage.py sqlmigrate 0003     # print the SQL (PostgreSQL)
```

Like Django, `makemigrations` compares your models with your *migration files*
(it replays them into a temporary database), not with your development
database, so you don't need to `migrate` first, and manual changes to your dev
database never sneak into a migration. Part 10 shows the safety checks it performs.

---

## 5. Schemas: what the API accepts and returns

Your models describe the database; **schemas** describe the API. Keeping them
separate is what stops `password` hashes or internal fields from leaking out.
`ModelSchema` builds a pydantic schema from a model's columns (types, lengths,
optional fields) so you don't repeat yourself; fields you declare yourself win.

`events/schemas.py`:

```python
"""Request and response schemas for events."""

from datetime import datetime

from fastapi_mvt.db import ModelSchema, Schema
from pydantic import Field, model_validator

from events.models import Event, Like, Registration
from users.schemas import UserSummary


class EventCreate(ModelSchema, model=Event, mode="create", exclude=["instructor_id"]):
    title: str = Field(min_length=3, max_length=200)

    @model_validator(mode="after")
    def ends_after_start(self) -> "EventCreate":
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be later than starts_at")
        return self


class EventUpdate(ModelSchema, model=Event, mode="update", exclude=["instructor_id"]):
    pass


class EventRead(ModelSchema, model=Event):
    pass


class RegistrationRead(ModelSchema, model=Registration):
    pass


class Attendee(Schema):
    registration_id: int
    registered_at: datetime
    student: UserSummary


class LikeIn(Schema):
    reaction: str = Field(default="like", min_length=1, max_length=30)


class LikeRead(ModelSchema, model=Like):
    pass


class LikeCount(Schema):
    event_id: int
    count: int
```

The three modes:

| Mode | Contains | Use for |
|---|---|---|
| `"read"` (default) | every public column | responses |
| `"create"` | what a client may set: no `id`, no timestamps; fields with defaults optional | POST bodies |
| `"update"` | like create, but everything optional | PATCH bodies |

Columns declared with `private(...)`, like the user's password hash, never appear
in any mode. Declaring a field yourself tightens its rules (`min_length=3` above),
and validators work as on any pydantic model. Type checkers understand the
`class X(ModelSchema, model=Event)` form, so mypy and your editor stay happy.

---

## 6. Endpoints

`events/router.py`. Read it top to bottom; there's no session handling anywhere:

```python
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status

from elearning.auth import CurrentUser, Instructor, Student
from events import services
from events.models import Event, Registration
from events.schemas import Attendee, EventCreate, EventRead, EventUpdate, RegistrationRead
from fastapi_mvt.db import Page, PageParams
from users.schemas import UserSummary

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=Page[EventRead])
async def list_events(params: PageParams = Depends(), q: str | None = None, upcoming: bool = False):
    events = Event.objects.order_by("starts_at")
    if q:
        events = events.filter(title__icontains=q)
    if upcoming:
        events = events.filter(starts_at__gte=datetime.now(timezone.utc))
    return await events.paginate(params)


@router.post("", response_model=EventRead, status_code=status.HTTP_201_CREATED)
async def create_event(data: EventCreate, instructor: Instructor):
    return await Event.objects.create(**data.model_dump(), instructor_id=instructor.id)


@router.get("/{event_id}", response_model=EventRead)
async def get_event(event_id: int):
    return await Event.objects.get(id=event_id)
```

Things to notice:

* **Queries read like Django.** `Event.objects.filter(title__icontains=q)` builds
  a query; `await` runs it. QuerySets are immutable, so building them
  conditionally (as above) is safe.
* **`.get()` returns 404 by itself.** It raises `Event.DoesNotExist`, which
  `db.init_app` maps to a 404 response. No `if not event: raise HTTPException`.
* **`paginate(params)`** returns `Page[...]`:
  `{"items": [...], "total": 42, "page": 1, "size": 20, "pages": 3, "has_next": true, ...}`.
  `?page=` and `?size=` are validated, with size capped at 100.

The query API in one table (full reference in [orm.md](orm.md)):

| Django | fastapi-mvt |
|---|---|
| `Event.objects.all()` | `await Event.objects.all()` |
| `.filter(title__icontains="sql", starts_at__gte=now)` | same |
| `.exclude(location=None)` | same |
| `.filter(Q(a=1) \| Q(b=2))` | same (`from fastapi_mvt.db import Q`) |
| `.filter(event__instructor__email="x")` | same (spans relations) |
| `.order_by("-starts_at")[:10]` | same |
| `.get(id=1)` / `.first()` / `.count()` / `.exists()` | `await ...` |
| `.select_related("event")` / `.prefetch_related("likes")` | same, or `.prefetch("likes")` |
| `.values("id", "title")` / `.values_list("id", flat=True)` | `await ...` |
| `.update(seats=F("seats") - 1)` | `.update(seats=Event.seats - 1)` (a plain SQLAlchemy expression) |
| `.create(...)`, `.get_or_create(...)`, `.update_or_create(...)`, `.bulk_create([...])` | `await ...` |
| `obj.save()` / `obj.delete()` / `obj.refresh_from_db()` | `await obj.save()` / `await obj.delete()` / `await obj.refresh()` |

---

## 7. Business rules and transactions

Rules like "only the owner may edit" belong in a plain module, not in the HTTP
layer. `events/services.py`:

```python
from fastapi import HTTPException, status

from events.models import Event, Like, Registration
from users.models import User


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, detail=detail)


async def get_own_event(event_id: int, instructor: User) -> Event:
    event = await Event.objects.get(id=event_id)  # 404 if missing
    if event.instructor_id != instructor.id:
        raise _forbidden("You can only manage your own events.")
    return event


async def update_event(event: Event, changes: dict) -> Event:
    starts_at = changes.get("starts_at", event.starts_at)
    ends_at = changes.get("ends_at", event.ends_at)
    if ends_at <= starts_at:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="ends_at must be later than starts_at")
    return await event.update(**changes)


async def register(event_id: int, student: User) -> Registration:
    event = await Event.objects.get(id=event_id)
    if event.instructor_id == student.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="You cannot register for your own event.")
    # No "already registered?" query: the unique constraint makes a duplicate a 409,
    # which is also correct when two requests race.
    return await Registration.objects.create(event_id=event.id, student_id=student.id)
```

And the endpoints that use them:

```python
@router.patch("/{event_id}", response_model=EventRead)
async def update_event(event_id: int, data: EventUpdate, instructor: Instructor):
    event = await services.get_own_event(event_id, instructor)
    return await services.update_event(event, data.model_dump(exclude_unset=True))


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(event_id: int, instructor: Instructor) -> None:
    event = await services.get_own_event(event_id, instructor)
    await event.delete()  # registrations and likes go with it (on_delete=CASCADE)


@router.post("/{event_id}/registrations", response_model=RegistrationRead, status_code=status.HTTP_201_CREATED)
async def register(event_id: int, student: Student):
    return await services.register(event_id, student)


@router.get("/{event_id}/registrations", response_model=list[Attendee])
async def attendees(event_id: int, instructor: Instructor):
    await services.get_own_event(event_id, instructor)
    registrations = await (
        Registration.objects.filter(event_id=event_id).prefetch("student").order_by("-created_at")
    )
    return [
        Attendee(registration_id=r.id, registered_at=r.created_at, student=UserSummary.model_validate(r.student))
        for r in registrations
    ]
```

### How transactions work (the one rule to remember)

**Inside a request, everything is one transaction.** It commits right before
the response is sent, and rolls back if the endpoint raises, including an
`HTTPException`, or returns an error status. So if a service writes two rows and
then raises `403`, neither row is saved. The client never receives a success
response for data that failed to commit.

Need a smaller unit inside a request? Use `atomic()`, which creates a savepoint:

```python
from fastapi_mvt.db import atomic

async with atomic():
    order = await Order.objects.create(...)
    await Stock.objects.filter(id=item_id).update(quantity=Stock.quantity - 1)
```

Outside requests (scripts, the shell, background tasks, WebSockets) each ORM call
commits on its own, like Django. Wrap a block in `async with atomic():` to
make it all-or-nothing.

---

## 8. Real-time likes with WebSockets

Likes use `update_or_create` (one row per user and event) and notify connected
browsers. The important part is **`on_commit`**: it runs a callback only after
the transaction commits. If the request fails, nobody is told about a like that
never happened.

`elearning/websocket.py`:

```python
from fastapi_mvt.websockets import ConnectionManager

manager = ConnectionManager()
```

Add to `events/services.py`:

```python
from elearning.websocket import manager
from fastapi_mvt.db import on_commit


async def like_count(event_id: int) -> int:
    return await Like.objects.filter(event_id=event_id).exclude(reaction="unlike").count()


async def set_reaction(event_id: int, user: User, reaction: str) -> Like:
    await Event.objects.get(id=event_id)
    like, _ = await Like.objects.update_or_create(event_id=event_id, user_id=user.id, defaults={"reaction": reaction})
    await _broadcast_likes(event_id)
    return like


async def remove_reaction(event_id: int, user: User) -> None:
    like = await Like.objects.get(event_id=event_id, user_id=user.id)
    await like.delete()
    await _broadcast_likes(event_id)


async def _broadcast_likes(event_id: int) -> None:
    count = await like_count(event_id)
    # Only tell websocket clients once the change is really committed.
    on_commit(lambda: manager.send_to_group(group=f"event:{event_id}", event="like_count", data={"count": count}))
```

And the endpoints:

```python
@router.put("/{event_id}/like", response_model=LikeRead)
async def like(event_id: int, user: CurrentUser, data: LikeIn | None = None):
    return await services.set_reaction(event_id, user, data.reaction if data else "like")


@router.websocket("/{event_id}/likes/ws")
async def live_likes(websocket: WebSocket, event_id: int):
    await manager.connect(websocket, group=f"event:{event_id}")
    try:
        await websocket.send_json({"type": "like_count", "data": {"count": await services.like_count(event_id)}})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)
```

A WebSocket may stay open for hours, so it does **not** hold a database
transaction; each ORM call inside it runs in its own short transaction. To run
several server processes, use `ConnectionManager(RedisChannelBackend(redis))`
(`pip install "fastapi-mvt[redis]"`) and call `await manager.start()` at startup.

---

## 9. Tests

```bash
pytest
```

fastapi-mvt gives you two fixtures, with no configuration:

* **`client`**: an HTTP client for your app.
* **`db`**: your database, inside a transaction that is rolled back after each
  test. Tests never see each other's data. Every `async def` test gets this
  automatically, even if it doesn't ask for `db`.

The test database is separate from your development database. For SQLite it's a
temporary file; for PostgreSQL, `<name>_test` is created and dropped, like Django
does. It's built **by running your migrations**, so broken migrations fail your
tests. If your models have changes without a migration, the run stops and tells you.

`tests/test_events.py` (excerpt):

```python
from datetime import datetime, timedelta, timezone

import pytest

from elearning.auth import auth
from events.models import Registration
from users.models import Role, User

START = datetime.now(timezone.utc) + timedelta(days=7)
EVENT = {"title": "Intro to SQL", "starts_at": START.isoformat(),
         "ends_at": (START + timedelta(hours=2)).isoformat()}


async def login_as(role: Role, email: str) -> dict[str, str]:
    user = await User.objects.create(email=email, password="!", role=role)
    return {"Authorization": f"Bearer {auth.create_access_token(user)}"}


@pytest.fixture
async def instructor(db):
    return await login_as(Role.INSTRUCTOR, "teacher@example.com")


@pytest.fixture
async def student(db):
    return await login_as(Role.STUDENT, "student@example.com")


async def test_registration_rules(client, instructor, student):
    event = (await client.post("/events", json=EVENT, headers=instructor)).json()
    url = f"/events/{event['id']}/registrations"
    assert (await client.post(url, headers=instructor)).status_code == 403
    assert (await client.post(url, headers=student)).status_code == 201
    assert (await client.post(url, headers=student)).status_code == 409   # unique constraint
    assert await Registration.objects.count() == 1
```

`password="!"` creates a user that cannot log in with a password, which is handy
in tests where you mint a token directly.

---

## 10. Changing the schema safely

Plain Alembic writes whatever diff it finds, even one that destroys data.
`makemigrations` checks the dangerous cases, as Django does.

**Renaming a field.** Rename `location` to `venue` in `Event` and run
`makemigrations`:

```
Was events.location renamed to events.venue? [y/N]: y
Migrations:
  migrations/versions/0004_rename_events_location.py
    - Rename column events.location to venue
```

On PostgreSQL, `python manage.py sqlmigrate 0004` shows exactly what will run:

```sql
ALTER TABLE events RENAME location TO venue;
```

Answer *no* and it becomes remove + add, which loses the data, so you're asked to
confirm that too. Renaming a **model class** renames its table; you get the same
question for the table.

**Adding a required field to a table with data:**

Add `capacity = IntegerField()` (required, no default) to `Event`:

```
Column events.capacity is NOT NULL with no database default. Existing rows in events need a value for it.
Enter a one-off value for existing rows (a Python literal, e.g. 0, 'draft', True), or leave empty to quit: 30
Migrations:
  migrations/versions/0005_events_capacity.py
    - Add column capacity to events
    - Alter column events.capacity (server default)
```

The migration fills existing rows with `30`, then removes the temporary default,
so your model stays the source of truth. Give the field a default instead
(`IntegerField(default=30)`) and, like Django, that default fills existing rows
without asking. Without a terminal (CI), a missing value is an error instead of
a migration that would crash on `migrate`.

**Deleting a model or field:**

```
These changes DELETE DATA:
  - Drop table 'likes': it is in the database but no model defines it. If you did not
    delete that model, make sure its module is a models.py inside an app package.
Write this migration anyway? [y/N]:
```

In CI or scripts (no terminal), these questions become errors unless you pass
`--allow-destructive`, so nothing destructive happens silently.

**A broken import can't drop your tables.** If a `models.py` fails to import,
every command stops with the original error. Old versions skipped the module,
which made its tables look deleted.

**Working in a team.** If two branches both add `0005_...`, `migrate` reports
the conflict and `makemigrations --merge` joins them.

**Data migrations.** `python manage.py makemigrations --empty --name backfill_slugs`
creates a migration with an example of updating rows.

More: [migrations.md](migrations.md).

---

## 11. Admin and production

### Admin site

```bash
pip install "fastapi-mvt[admin]"
python manage.py createsuperuser
```

In `main.py`:

```python
from fastapi_mvt.admin import setup_admin

setup_admin(app, db, auth=auth)   # /admin, superusers only
```

Every model gets list/detail/edit pages (via [SQLAdmin](https://aminalaee.dev/sqladmin/)).
Password hashes are never shown.

### Login protection and password reset

Five wrong passwords in a row lock an account for 15 minutes
(`Auth(..., max_failed_logins=5, lockout_minutes=15)`); the counter lives in the
database, so it works across server processes. To add
`/auth/password-reset`, pass a function that delivers the token:

```python
async def send_password_reset(user: User, token: str) -> None:
    await send_email(user.email, "Reset your password", f"https://app.example.com/reset?token={token}")

auth = ElearningAuth(User, secret_key=settings.secret_key, send_password_reset=send_password_reset, ...)
```

### PostgreSQL

```bash
pip install -e ".[postgres]"
```

```ini
# .env
DATABASE_URL=postgresql://elearning:secret@localhost:5432/elearning
```

Then run `python manage.py migrate`. The async driver is selected for you.

### Deploying

1. Set environment variables: `SECRET_KEY` (required; the app refuses to start
   without it), `DATABASE_URL`, `ALLOWED_ORIGINS`, `DEBUG=false`.
2. On each release, run `python manage.py migrate` **before** starting the new code.
3. Start the server with several workers:
   `uvicorn elearning.main:app --host 0.0.0.0 --port 8000 --workers 4`.
4. In CI, run `python manage.py check` and `pytest`. `check` fails when
   migrations are missing or unapplied.

More: [deployment.md](deployment.md).

---

## Where next

* [ORM guide](orm.md): every query method, relationships, custom QuerySets, raw SQLAlchemy.
* [Migrations guide](migrations.md): commands, safety checks, teams, production.
* [Testing guide](testing.md)
* [Auth and admin](auth-and-admin.md)
* [Design notes](design.md): why fastapi-mvt is built this way.
