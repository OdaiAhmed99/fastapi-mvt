# Design notes

Why fastapi-mvt is built the way it is. This answers the fair questions asked
about version 0.1: *why not SQLModel? why not a template? isn't this a
Django-like monolith?*

## What it is

**A library, not a framework around FastAPI.** Your app is a normal FastAPI app.
fastapi-mvt adds:

1. a Django-style data layer on SQLAlchemy 2 (models, QuerySets, transactions),
2. a safe Django-style migration workflow on Alembic,
3. a test harness (isolated test database, per-test rollback),
4. a CLI to scaffold projects and apps,
5. optional extras: auth, admin, WebSocket fan-out.

Remove it and you're left with FastAPI + SQLAlchemy + Alembic code that still
makes sense.

## Why SQLAlchemy, and not SQLModel

SQLModel is SQLAlchemy plus shared pydantic/table classes. It improves how
models are *declared*, but the everyday pain is elsewhere: sessions, async
loading errors, transactions, query verbosity, migrations and testing. SQLModel
doesn't address those. Its own docs also recommend separate `Create`/`Read`/
`Update` classes to avoid leaking fields, which is what `ModelSchema`
generates for you.

Building a new ORM would mean re-implementing SQL generation, relationships and
dialects, a maintenance burden with no end. So the rule is:

> Every model is a real SQLAlchemy mapped class, and every QuerySet compiles to a
> real `select()`, exposed as `qs.statement`.

The Django-style layer is thin (~2k lines) and translates to SQLAlchemy calls
that are easy to inspect. Alembic, SQLAdmin, and any SQLAlchemy extension work
unchanged, and dropping to SQLAlchemy is ordinary usage, not an escape hatch.

## Decisions in the data layer

| Decision | Why |
|---|---|
| One transaction per request, committed *before* the response is sent | Django's `ATOMIC_REQUESTS` semantics; a client never gets 200 for data that failed to commit |
| Error status (≥400) or exception → rollback | `raise HTTPException(403)` after a write can't leave half-done work |
| Autocommit per call outside requests | What Django does; scripts, the shell and background tasks just work |
| `on_commit` instead of ORM signals | Signals fired during flush ran for rows that were later rolled back (verified in 0.1) |
| Relationships never load implicitly | Implicit I/O is impossible in async and causes N+1 in sync; failing loudly is safer |
| Deletes executed by the database (`on_delete` required) | Correct for bulk deletes, no loading of children, same semantics as Django's API |
| SQLite: foreign keys on, real connection pool, savepoints fixed | 0.1 silently ignored `CASCADE` and shared one connection between concurrent sessions |
| Timestamps always UTC-aware | SQLite would otherwise return naive datetimes and store wall-clock times |
| Enums stored as VARCHAR values | Adding a member needs no `ALTER TYPE`; readable in SQL |

## Decisions in migrations

Alembic is the standard, so migration files are Alembic scripts. What
fastapi-mvt adds is what Django developers rely on and Alembic leaves to you:

- comparing models with the **migration history** (replayed into a scratch
  database), not with the dev database, so pending migrations and manual
  changes never leak into a new migration;
- rename detection, confirmation before data loss, defaults for new required
  fields (the field's default, or a one-off value);
- sequential names, backwards `migrate <target>`, merge for branches, a CI check;
- refusing to continue when a models module fails to import (0.1 swallowed that
  error and could generate `DROP TABLE`).

## Why a CLI and not only a template

A template (Cookiecutter/Copier) is great for the *first* commit, and the
generated project here is intentionally plain: you own every file. But the
valuable commands (`makemigrations`, `migrate`, `showmigrations`, `check`,
`createsuperuser`) are behaviour, not files, and behaviour in a template can't
be upgraded or tested. So scaffolding lives in real template files inside the
package (not f-strings), and the CLI carries the tested behaviour.

## What was removed from 0.1, and why

| Removed | Reason |
|---|---|
| `INSTALLED_APPS` / app registry | Silently skipped apps that failed to import; routers are now included explicitly |
| ORM signals | Fired before commit, ran for rolled-back data, blocked or lost async receivers → `on_commit` |
| SQL-injection middleware | Blocked legitimate input (`rock;roll`, `Tom and Jerry`) with 500s and protected nothing (queries are parameterised) |
| In-process rate limiter | 500 instead of 429, bypassable via `X-Forwarded-For`, per-process; use a proxy or `slowapi` |
| CORS `*` with credentials default | Let any site make credentialed requests |
| Celery, Redis, Jinja2, websockets as hard dependencies | Not every project needs them; Celery works with `on_commit` + `db.run` |
| In-memory token blacklist | Didn't work with more than one process → `token_version` |
| `manage.py` logic / 1,000-line f-string generator | Replaced by real template files and a tested CLI |

## Developer experience decisions

| Decision | Why |
|---|---|
| Django-style field helpers (`CharField(max_length=...)`) next to `Mapped[...]` | Familiar to Django developers, and still fully typed SQLAlchemy columns |
| `class X(ModelSchema, model=M)` instead of `class X(model_schema(M))` | Type checkers understand class keywords; dynamic base classes fail mypy |
| Ships `py.typed`; generated code passes ruff and mypy | Teams with CI linting shouldn't fail on untouched scaffolding |
| Relationship errors say how to fix them | SQLAlchemy's `lazy='raise_on_sql'` message tells you what, not what to do |
| `async_to_sync` for `def` endpoints and scripts | Gradual adoption in sync codebases, with the same request transaction |
| `commit_on_error()` | The escape hatch for writes that must survive an error response |
| Per-test isolation for every async test | Forgetting a fixture must not make tests order-dependent |
| App `commands.py` becomes `manage.py` commands | Django developers expect to write their own commands |

## What is deliberately *not* included

Templates/HTML rendering, forms, an admin framework of its own, a settings
framework beyond pydantic-settings, and a plugin/app registry. FastAPI and its
ecosystem already solve those well.
