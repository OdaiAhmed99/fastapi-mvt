# Choosing fastapi-mvt (or not)

An honest comparison with the tools you're probably also considering. Each one
is good; they solve different problems.

## The short answer

| If you… | Use |
|---|---|
| already run Django, or want Django's admin, auth, forms and ecosystem with a typed API layer | **Django Ninja** |
| want the official, complete FastAPI starting point (React frontend, Docker, Traefik) | **FastAPI full-stack template** |
| want the thinnest layer over SQLAlchemy, with one class for table and schema | **SQLModel** |
| want a different async framework with a batteries-included data layer | **Litestar + Advanced Alchemy** |
| want **FastAPI + SQLAlchemy**, but with Django's everyday data-layer productivity: queries, transactions, safe migrations, test isolation | **fastapi-mvt** |

## fastapi-mvt vs Django Ninja

Django Ninja puts a FastAPI-style API layer on top of Django. **If you want
Django, use Django**: its ORM, admin, auth, migrations and ecosystem are more
mature than anything here, and Django Ninja gives it a modern API layer. For a
Django team building an API, it's the better choice.

fastapi-mvt is for the opposite starting point: teams on **FastAPI and
SQLAlchemy** who want Django's conveniences without leaving that stack.

| | Django Ninja | fastapi-mvt |
|---|---|---|
| Web framework | Django (ASGI or WSGI) | FastAPI / Starlette |
| ORM | Django ORM (async support is partial; many operations run in a thread) | SQLAlchemy 2, async end to end |
| Query power | Django ORM | Django-style API, plus all of SQLAlchemy one line away |
| Migrations | Django migrations (the gold standard) | Alembic, with Django-style commands and safety checks |
| Admin | Django admin (excellent) | SQLAdmin (good, less customizable) |
| Auth | Django auth + packages | JWT auth with lockout and reset; OAuth via other libraries |
| Ecosystem | Django packages | FastAPI + SQLAlchemy packages |
| Maturity | Established | New (0.x) |

## fastapi-mvt vs SQLModel

SQLModel merges the SQLAlchemy table and the pydantic schema into one class, and
it's what FastAPI's own tutorial uses. It improves how models are declared, but
leaves the rest to you: sessions, query syntax, transactions, migrations and
test setup. Its docs also recommend separate `Create`/`Read`/`Update` classes to
avoid leaking fields, which `ModelSchema` generates here.

| | SQLModel | fastapi-mvt |
|---|---|---|
| Model declaration | one class for table + schema | model + `ModelSchema` generated from it |
| Queries | `session.exec(select(Hero).where(Hero.age > 30))` | `await Hero.objects.filter(age__gt=30)` |
| Sessions | you pass them around | ambient; one transaction per request |
| Migrations | plain Alembic | Django-style commands + safety checks |
| Test isolation | set it up yourself | built in |
| Typing | excellent | excellent for fields; lookups like `age__gt` are strings |

## fastapi-mvt vs the FastAPI full-stack template

The official template is a complete *application*: React frontend, Docker
Compose, Traefik, email, CI. It's the best way to start a full product with
FastAPI's author's choices. fastapi-mvt is a *library* plus a small backend
project generator. The two can coexist: the template's backend could use
fastapi-mvt's data layer.

## fastapi-mvt vs plain SQLAlchemy

Plain SQLAlchemy 2 is the most flexible option and what fastapi-mvt is built
on. You give up nothing by adding fastapi-mvt: every model is a SQLAlchemy
model, `qs.statement` is a SQLAlchemy `Select`, and `current_session()` is a
real `AsyncSession`. What you gain is less code for common work, request
transactions, migration safety checks and test isolation. What it costs is a
dependency, a young project, and about 0.1 ms of Python overhead per query (see
[Performance](orm.md#performance)).

## When fastapi-mvt is the wrong choice

- You need Django's admin, forms or ecosystem → Django (with Django Ninja).
- Your codebase is sync-only and large → `async_to_sync` works, but the ORM is
  designed for async code.
- You need several databases, read replicas or schema-per-tenant → not
  supported yet; use SQLAlchemy directly.
- You need MySQL today → only SQLite and PostgreSQL are tested.
- You can't take a dependency on a 0.x project → wait for 1.0, or use plain
  SQLAlchemy and borrow the patterns.
