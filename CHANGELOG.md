# Changelog

## 0.2.0 (unreleased)

A redesign. See [Upgrading from 0.1](docs/upgrading-from-0.1.md) and
[Design notes](docs/design.md).

### Added

- Django-style async ORM layer on SQLAlchemy 2: `Model`, `TimestampedModel`,
  `Model.objects` QuerySets with Django lookups (including across relations),
  `Q`, `get_or_create`/`update_or_create`, bulk operations, `values()`,
  `aggregate()`, custom QuerySets, `Model.DoesNotExist` → 404, `IntegrityError` → 409.
- One transaction per HTTP request (committed before the response is sent),
  `atomic()` with savepoints, `on_commit()`, `current_session()`, `db.run()` for sync code.
- `fk(..., on_delete=...)`, async-safe `relationship()`, `private()` columns,
  UTC-aware timestamps, enums stored as values.
- `model_schema()` to derive create/read/update pydantic schemas; `Page`/`PageParams`.
- Migration commands: `makemigrations` (rename detection, destructive-change
  confirmation, one-off defaults for new NOT NULL columns, `--empty`, `--merge`,
  `--check`), `migrate [target|zero] [--fake]`, `rollback`, `showmigrations`, `sqlmigrate`.
- `check`, `shell` (top-level `await`), `routes`, `runserver`, `startapp --crud`.
- pytest plugin: `db` and `client` fixtures, test database built from migrations,
  per-test rollback, automatic `<name>_test` PostgreSQL database.
- Auth rewrite: async, PyJWT, Django-compatible PBKDF2 hashes, token revocation
  via `token_version` (works across processes), refresh tokens, password change,
  OAuth2 form endpoint for `/docs`.
- `setup_admin()` for SQLAdmin, superusers only.
- Test suite (SQLite and PostgreSQL) and CI.

### Added (developer experience)

- Django-style field helpers: `CharField`, `TextField`, `IntegerField`,
  `DecimalField`, `BooleanField`, `DateTimeField(auto_now_add=...)`, `EnumField`,
  `ForeignKey`, and more, typed as `Mapped[...]`.
- `ModelSchema` (`class PostRead(ModelSchema, model=Post)`), understood by type checkers.
- `py.typed`: the package is typed; generated projects pass ruff and mypy.
- `makemigrations` compares models with the migration history (scratch
  database) instead of the dev database; no `migrate` needed first.
- A new NOT NULL column with a default fills existing rows automatically.
- Relationship errors (`RelationNotLoaded`) explain how to load the relation.
- Custom management commands from each app's `commands.py`.
- `async_to_sync` for plain `def` endpoints and scripts.
- `commit_on_error()` and `db.init_app(app, rollback_on_error_status=False)`.
- Every async test is isolated automatically (`@pytest.mark.no_db` opts out).
- Auth: login lockout after repeated failures (429) and password reset endpoints.
- Docs: comparison page, CLI reference, benchmark, HTML docs site (GitHub Pages).

### Changed

- Projects are generated from real template files; `startproject`/`startapp`
  never overwrite files. Project configuration lives in `[tool.fastapi-mvt]` in `pyproject.toml`.
- SQLite: foreign keys enforced, a real connection pool, correct SAVEPOINTs.
- Core dependencies trimmed to FastAPI, SQLAlchemy, Alembic, pydantic-settings, Typer
  and aiosqlite; everything else is an extra.

### Removed

- `INSTALLED_APPS` / `AppRegistry`: include routers explicitly.
- ORM signals: use `on_commit()` and explicit calls.
- `SQLInjectionProtectionMiddleware`, `RateLimitMiddleware`, `CustomMiddleware`.
- Hard dependencies on Celery, Redis, Jinja2, websockets, python-jose, bcrypt.
- `get_object_or_404`, `get_list_or_404`, `paginate()` helpers (replaced by QuerySet methods).

### Fixed (found in the 0.2 production review)

- Concurrent ORM calls in one request (`asyncio.gather`) failed with a 500;
  they now take turns on the request's session.
- With Alembic 1.14 a detected column rename was written as a no-op
  `alter_column`; renames are now always rendered (and never silently dropped).
- `typer>=0.12` allowed versions that crash with current Click; the minimum is now 0.13.
- `migrate` and `check` created scratch databases on the server being deployed
  to; they now compare with the (up-to-date) live database.
- An explicit `null` for a required field in an update schema produced a 409
  from the database; it's now a 422 validation error.
- Server databases now use `pool_pre_ping`, so a database restart doesn't break
  the next requests.
- `ModelSchema` no longer rewrites class annotations (future-proof for Python 3.14).
- `on_commit(async_fn)` from plain sync code crashed; it now runs immediately.

### Fixed (0.1 issues)

- A models module failing to import could generate `DROP TABLE` migrations.
- `makemigrations` silently upgraded the database; `createsuperuser` created
  tables outside migrations.
- Concurrent SQLite sessions shared one connection and each other's transactions.
- `ON DELETE CASCADE` was ignored on SQLite.
- Signals fired for rolled-back data.
- Wide-open CORS with credentials by default.
