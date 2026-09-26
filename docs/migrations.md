# Migrations guide

fastapi-mvt gives you Django's migration commands on top of
[Alembic](https://alembic.sqlalchemy.org/), the standard SQLAlchemy migration
tool. Migration files are ordinary Alembic scripts: readable, editable and
reviewable in pull requests.

## Commands

| Command | What it does |
|---|---|
| `makemigrations` | Compare models with the database and write a new migration |
| `makemigrations --name add_prices` | Choose the name |
| `makemigrations --empty --name backfill` | Empty migration for data changes |
| `makemigrations --check` | Exit 1 if models have unmigrated changes (CI) |
| `makemigrations --merge` | Join two branches' migrations |
| `makemigrations --no-input` | Never prompt; unsafe changes become errors |
| `makemigrations --allow-destructive` | Allow drops without asking |
| `migrate` | Apply everything pending |
| `migrate 0003` | Move to 0003, forwards or **backwards** |
| `migrate zero` | Unapply everything |
| `migrate --fake` | Mark as applied without running (adopting an existing database) |
| `rollback [n]` | Unapply the last *n* migrations (default 1) |
| `showmigrations` | `[X]` applied / `[ ]` pending |
| `sqlmigrate 0003 [--backwards]` | Print the SQL (PostgreSQL/MySQL; SQLite needs a live database for batch mode) |
| `check` | Config, models and migrations are all in order (CI) |

Run them as `python manage.py <command>` or `fastapi-mvt <command>` from anywhere
inside the project. Migrations are numbered like Django's: `0001_initial`,
`0002_users_role`, `0003_create_events_and_more`.

## The workflow

1. Change a model.
2. `python manage.py makemigrations`: review the printed operations, and the file if unsure.
3. `python manage.py migrate`.
4. Commit the model change **and** the migration file together.

`migrate` warns when your models have changes that no migration covers.

**What `makemigrations` compares against.** Like Django, it compares your
models with your *migration files*, not with your development database: it
replays the migrations into a temporary database (a temp SQLite file, or a
throwaway PostgreSQL database) and diffs against that. So you never need to
`migrate` before `makemigrations`, switching git branches doesn't confuse it,
and a table you created by hand in your dev database never sneaks into a
migration. It also proves your existing migrations still apply from scratch.
If it can't create a PostgreSQL database (no `CREATEDB` permission), it falls
back to comparing against your database and says so.

## Safety checks

Alembic's autogenerate is a diff; it can't know your intent. `makemigrations`
adds the checks that prevent data loss:

**Renames.** A removed column plus an added column of a compatible type produces:

```
Was events.location renamed to events.venue? [y/N]
```

*Yes* writes `ALTER TABLE ... RENAME`, keeping the data; the downgrade renames it
back. Renaming a model class renames its table, and gets the same question.
Without a terminal, a possible rename is reported and the drop needs `--allow-destructive`.

**Deletions.** Dropping a table or a column asks for confirmation (or needs
`--allow-destructive`). A dropped table also gets a hint: if you didn't delete the
model, its module isn't being imported.

**Required fields on existing tables.** A new NOT NULL column needs a value for
the rows that already exist, or `migrate` would crash in production:

* if the field has a default (`IntegerField(default=0)`), that default fills
  the existing rows automatically, as in Django;
* otherwise you're asked for a one-off value, which fills the rows and is then
  removed from the schema.

Making a nullable column NOT NULL works the same way (an `UPDATE` replaces NULLs
first). Without a terminal, a missing value is an error that explains the options.

**Broken imports.** Every `models.py` is imported before comparing. If one fails,
the command stops with the original traceback. A model that silently failed to
import would otherwise look deleted and produce a `DROP TABLE`.

**No hidden writes.** `makemigrations` never changes your database; only `migrate`
does. There is no `create_all()` anywhere, so migrations are the single source
of truth for the schema.

## Which models are included

With `models = "auto"` in `[tool.fastapi-mvt]` (the default), every top-level
package containing a `models.py` or a `models/` package is imported. To be
explicit instead:

```toml
[tool.fastapi-mvt]
models = ["users.models", "events.models", "billing.models"]
```

## Data migrations

```bash
python manage.py makemigrations --empty --name fill_slugs
```

```python
def upgrade() -> None:
    posts = sa.table("posts", sa.column("title", sa.String), sa.column("slug", sa.String))
    op.execute(posts.update().values(slug=sa.func.lower(posts.c.title)))
```

Use `sa.table()` snapshots rather than importing your models: the migration must
keep working after the models change.

## SQLite and PostgreSQL

* Migrations generated on SQLite apply on PostgreSQL and vice versa. The test
  suite runs every scenario on both.
* On SQLite, table changes use Alembic's *batch mode* (copy the table, change
  it, copy back), which SQLite needs for most `ALTER`s. This is automatic.
* Each migration runs in its own transaction. If migration 0005 fails, 0001–0004
  stay applied and 0005 is rolled back (PostgreSQL rolls back DDL too; SQLite
  mostly does).

## Working in a team

If two branches each add `0005_...`, both are "latest":

```
Error: Conflicting migrations detected; multiple leaf migrations: 0005_add_price, 0005_add_sku.
Run `makemigrations --merge`.
```

`makemigrations --merge` writes `0006_merge_0005_0005`, which depends on both.

## In production

* Run `python manage.py migrate` as a release step, **before** starting the new code.
* Prefer backwards-compatible steps for zero-downtime deploys: add a nullable
  column, deploy, backfill, then make it required in a later release.
* `python manage.py check` in CI catches forgotten migrations.

## Adopting an existing database

If the tables already exist (created by hand or by fastapi-mvt 0.1's
`create_all`), create the initial migration and mark it as applied:

```bash
python manage.py makemigrations    # against an EMPTY database, e.g. DATABASE_URL=sqlite:///tmp.db
python manage.py migrate --fake    # on the real database: records it as applied, runs nothing
```

Then run `makemigrations` against the real database to see any remaining differences.

## Using Alembic directly

Migration files are standard Alembic scripts, and `migrations/env.py` simply calls
`fastapi_mvt.migrations.run_migrations()`. Generate migrations with
`makemigrations`, though: the safety checks live there, not in plain
`alembic revision --autogenerate`.
