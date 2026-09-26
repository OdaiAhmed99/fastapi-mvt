# Command line

Run commands as `python manage.py <command>` or `fastapi-mvt <command>` from
anywhere inside a project. `--help` works on every command.

## Project

| Command | |
|---|---|
| `startproject <name> [-d DIR]` | New project: settings, database, auth, tests. Generates a random `SECRET_KEY` |
| `startapp <name>` | New app (models, schemas, router, test) and registers its router in `main.py` |
| `startapp <name> --crud <Model>` | Same, plus a working model, schemas, CRUD endpoints and tests |

Generators never overwrite files, and the generated code passes ruff and mypy.

## Migrations

| Command | |
|---|---|
| `makemigrations [--name N] [--empty] [--merge] [--check] [--allow-destructive] [--no-input]` | Write a migration for model changes |
| `migrate [target] [--fake]` | Apply migrations; `migrate 0003` moves back or forward, `migrate zero` unapplies all |
| `rollback [n]` | Unapply the last *n* migrations |
| `showmigrations` | List migrations, `[X]` if applied |
| `sqlmigrate <migration> [--backwards]` | Print a migration's SQL |

See the [migrations guide](migrations.md).

## Development

| Command | |
|---|---|
| `runserver [host:port] [--noreload]` | Development server with auto-reload |
| `shell` | Python shell with every model and `db` loaded; top-level `await` works (IPython if installed) |
| `createsuperuser [--email E] [--no-input]` | Create an admin user (`--no-input` reads `$SUPERUSER_PASSWORD`) |
| `routes` | List the app's endpoints |
| `check` | Exit 1 if the app doesn't import, models are broken, or migrations are missing or unapplied (for CI) |
| `version` | Print the fastapi-mvt version |

## Your own commands

Like Django's `management/commands`: put a `commands.py` in any app, and every
function decorated with `@command()` becomes a command. Parameters become
options (with type conversion and `--help`), and the docstring becomes the help
text. Async commands run inside one database transaction.

```python
# events/commands.py
from fastapi_mvt.commands import command
from fastapi_mvt.db import utcnow

from events.models import Event


@command()
async def close_past_events(dry_run: bool = False) -> None:
    """Close events that have ended."""
    past = Event.objects.filter(ends_at__lt=utcnow())
    print(f"{await past.count()} event(s) to close")
    if not dry_run:
        await past.delete()


@command(name="hello")
def say_hello(name: str) -> None:
    print(f"hello {name}")
```

```bash
python manage.py close_past_events --dry-run
python manage.py hello Ada
```

A broken `commands.py` prints a warning instead of blocking the built-in
commands. A command can't reuse a built-in command's name.
