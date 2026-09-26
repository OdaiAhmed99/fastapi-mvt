"""The ``fastapi-mvt`` / ``python manage.py`` command line."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Optional

import typer

from fastapi_mvt import __version__

app = typer.Typer(
    help="fastapi-mvt: Django-style commands for FastAPI projects.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)


def _ok(text: str) -> None:
    typer.secho(text, fg=typer.colors.GREEN)


def _warn(text: str) -> None:
    typer.secho(text, fg=typer.colors.YELLOW, err=True)


def _interactive(no_input: bool) -> bool:
    return not no_input and sys.stdin.isatty()


def _prompter(no_input: bool) -> Any:
    from fastapi_mvt.migrations.autogen import Prompter

    # Prompts go to stderr: they are shown while Alembic's stdout is captured.
    return Prompter(
        interactive=_interactive(no_input),
        confirm=lambda text: typer.confirm(text, default=False, err=True),
        ask=lambda text: typer.prompt(text, default="", show_default=False, err=True),
    )


def _project() -> Any:
    from fastapi_mvt.project import get_project

    return get_project()


# ── Scaffolding ─────────────────────────────────────────────────────────────


@app.command()
def startproject(
    name: str = typer.Argument(..., help="Project package name, e.g. 'shop'."),
    directory: Path = typer.Option(Path("."), "--directory", "-d", help="Where to create the project folder."),
) -> None:
    """Create a new project."""
    from fastapi_mvt.scaffold import start_project

    target = start_project(name, directory)
    _ok(f"Created project {name!r} in {target}")
    typer.echo(
        "\nNext steps:\n"
        f"  cd {target}\n"
        "  python -m venv .venv  and activate it\n"
        '  pip install -e ".[test]"\n'
        "  python manage.py makemigrations\n"
        "  python manage.py migrate\n"
        "  python manage.py runserver"
    )


@app.command()
def startapp(
    name: str = typer.Argument(..., help="App package name, e.g. 'blog'."),
    crud: Optional[str] = typer.Option(
        None, "--crud", metavar="MODEL", help="Also generate a model, schemas, a CRUD router and tests, e.g. --crud Post."
    ),
) -> None:
    """Create an app inside the current project and register its router."""
    from fastapi_mvt.scaffold import start_app

    project = _project()
    result = start_app(name, project.root, project.package, crud_model=crud)
    _ok(f"Created app {name!r}:")
    for path in result.files:
        typer.echo(f"  {path.relative_to(project.root).as_posix()}")
    if result.router_registered:
        typer.echo(f"Registered the router in {project.package}/main.py.")
    else:
        _warn("Add the router to your app:\n  " + "\n  ".join(result.router_lines))
    if crud:
        typer.echo("Now run: python manage.py makemigrations && python manage.py migrate")


# ── Migrations ──────────────────────────────────────────────────────────────


@app.command()
def makemigrations(
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Name for the migration (default: generated)."),
    empty: bool = typer.Option(False, "--empty", help="Create an empty migration for data changes."),
    merge: bool = typer.Option(False, "--merge", help="Merge conflicting migrations from different branches."),
    check: bool = typer.Option(False, "--check", help="Exit 1 if models have changes without a migration (CI)."),
    allow_destructive: bool = typer.Option(
        False, "--allow-destructive", help="Allow dropping tables/columns without asking."
    ),
    no_input: bool = typer.Option(False, "--no-input", "--noinput", help="Never prompt."),
) -> None:
    """Detect model changes and write a new migration."""
    from fastapi_mvt.migrations import commands

    project = _project()
    if merge:
        path = commands.merge(project)
        if path:
            _ok(f"Created merge migration {path}")
        else:
            typer.echo("No conflicts to merge.")
        return
    if check:
        commands.ensure_migrations_dir(project)
        if commands.has_unmigrated_changes(project):
            _warn("Your models have changes that are not reflected in a migration. Run makemigrations.")
            raise typer.Exit(1)
        typer.echo("No changes detected.")
        return

    result = commands.makemigrations(
        project, name=name, empty=empty, allow_destructive=allow_destructive, prompter=_prompter(no_input)
    )
    if result.created_directory:
        typer.echo(f"Created {project.migrations_dir.as_posix()}/")
    for warning in result.plan.warnings:
        _warn(f"Note: {warning}")
    if result.plan.no_changes:
        typer.echo("No changes detected.")
        return
    typer.secho("Migrations:", bold=True)
    typer.secho(f"  {result.path}", fg=typer.colors.CYAN)
    for line in result.plan.operations or ["(empty: edit it to add operations)"]:
        typer.echo(f"    - {line}")


@app.command()
def migrate(
    target: Optional[str] = typer.Argument(
        None, help="Migration to move to, e.g. 0003 (backwards works), or 'zero' to unapply all."
    ),
    fake: bool = typer.Option(False, "--fake", help="Mark as applied without running (adopt an existing database)."),
) -> None:
    """Apply (or unapply) migrations."""
    from fastapi_mvt.migrations import commands

    project = _project()
    if fake:
        from alembic import command as alembic_command

        cfg = commands.alembic_config(project)
        script = commands.ScriptDirectory.from_config(cfg)
        alembic_command.stamp(cfg, commands.resolve_revision(script, target or "head"))
        _ok(f"Marked {target or 'all migrations'} as applied (nothing was run).")
        return

    typer.secho("Running migrations:", bold=True)

    def on_step(revision: str, upgrade: bool) -> None:
        typer.echo(f"  {'Applying' if upgrade else 'Unapplying'} {revision}... ", nl=False)
        typer.secho("OK", fg=typer.colors.GREEN)

    try:
        result = commands.migrate(project, target, on_step=on_step)
    except commands.MigrationError:
        raise
    except Exception:
        typer.secho("  FAILED", fg=typer.colors.RED)
        _warn("Migrations listed as OK above were committed; the failing one was rolled back.")
        raise
    if not result.applied and not result.unapplied:
        typer.echo("  No migrations to apply.")
    if result.model_changes_pending:
        _warn(
            "  Your models have changes that are not yet reflected in a migration, and so won't be applied.\n"
            "  Run `makemigrations` to make new migrations, and then re-run `migrate` to apply them."
        )


@app.command()
def rollback(steps: int = typer.Argument(1, min=1, help="How many migrations to unapply.")) -> None:
    """Unapply the most recent migration(s)."""
    from fastapi_mvt.migrations import commands

    typer.secho("Rolling back:", bold=True)
    commands.rollback(
        _project(), steps, on_step=lambda rev, up: typer.echo(f"  Unapplying {rev}... OK")
    )


@app.command()
def showmigrations() -> None:
    """List migrations and whether each is applied."""
    from fastapi_mvt.migrations import commands

    states = commands.showmigrations(_project())
    if not states:
        typer.echo("(no migrations)")
    for state in states:
        mark = typer.style("[X]", fg=typer.colors.GREEN) if state.applied else "[ ]"
        typer.echo(f" {mark} {state.revision}  {state.doc}")


@app.command()
def sqlmigrate(
    target: str = typer.Argument(..., help="Migration, e.g. 0002"),
    backwards: bool = typer.Option(False, "--backwards", help="Show the SQL to unapply it."),
) -> None:
    """Print the SQL a migration would run."""
    from fastapi_mvt.migrations import commands

    typer.echo(commands.sqlmigrate(_project(), target, backwards=backwards))


# ── Development ─────────────────────────────────────────────────────────────


@app.command()
def runserver(
    address: str = typer.Argument("127.0.0.1:8000", help="host:port or port"),
    reload: bool = typer.Option(True, "--reload/--noreload", help="Restart on code changes."),
) -> None:
    """Start the development server."""
    try:
        import uvicorn
    except ImportError:
        raise typer.BadParameter("uvicorn is not installed: pip install 'fastapi-mvt[server]'") from None
    project = _project()
    host, _, port = address.rpartition(":")
    typer.echo(f"Starting development server at http://{host or '127.0.0.1'}:{port}/docs")
    uvicorn.run(
        project.app,
        host=host or "127.0.0.1",
        port=int(port),
        reload=reload,
        reload_dirs=[str(project.root)] if reload else None,
        app_dir=str(project.root),
    )


@app.command()
def shell() -> None:
    """Python shell with models and `db` loaded. Top-level `await` works."""
    from fastapi_mvt.db.models import Base
    from fastapi_mvt.project import load_database, load_project_models

    project = _project()
    load_project_models(project)
    namespace: dict[str, Any] = {m.class_.__name__: m.class_ for m in Base.registry.mappers}
    namespace["db"] = load_database(project)
    import fastapi_mvt.db as orm

    namespace.update({name: getattr(orm, name) for name in ("atomic", "Q", "current_session")})
    banner = "Loaded: " + ", ".join(sorted(namespace)) + "\nExample: await User.objects.count()"
    try:
        from IPython import start_ipython

        start_ipython(argv=["--no-banner"], user_ns=namespace, display_banner=False)
        return
    except ImportError:
        pass
    typer.echo(banner)
    _asyncio_repl(namespace)


def _asyncio_repl(namespace: dict[str, Any]) -> None:
    import ast
    import code
    import inspect
    import types

    loop = asyncio.new_event_loop()

    class Console(code.InteractiveConsole):
        def runcode(self, codeobj: types.CodeType) -> None:
            try:
                func = types.FunctionType(codeobj, self.locals)
                result = func()
                if inspect.iscoroutine(result):
                    loop.run_until_complete(result)
            except SystemExit:
                raise
            except BaseException:
                self.showtraceback()

    console = Console(namespace)
    console.compile.compiler.flags |= ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
    console.interact(banner="", exitmsg="")


@app.command()
def createsuperuser(
    email: Optional[str] = typer.Option(None, "--email", help="Email address."),
    no_input: bool = typer.Option(False, "--no-input", "--noinput", help="Read the password from $SUPERUSER_PASSWORD."),
) -> None:
    """Create a user with admin rights."""
    from fastapi_mvt.auth import AbstractUser
    from fastapi_mvt.db.models import Base
    from fastapi_mvt.project import load_database, load_project_models

    project = _project()
    load_project_models(project)
    user_models = [m.class_ for m in Base.registry.mappers if issubclass(m.class_, AbstractUser)]
    if len(user_models) != 1:
        raise typer.BadParameter(f"Expected one model based on AbstractUser, found {len(user_models)}.")
    User: Any = user_models[0]
    database = load_database(project)

    if no_input:
        password = os.environ.get("SUPERUSER_PASSWORD", "")
        if not email or not password:
            raise typer.BadParameter("--no-input needs --email and the SUPERUSER_PASSWORD environment variable.")
    else:
        email = email or typer.prompt("Email address")
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    if len(password) < 8:
        raise typer.BadParameter("The password must be at least 8 characters.")

    async def create() -> Any:
        from sqlalchemy.exc import OperationalError, ProgrammingError

        try:
            async with database.session():
                if await User.objects.filter(email=email.lower()).exists():
                    raise typer.BadParameter(f"A user with email {email!r} already exists.")
                user = User(email=email.lower(), is_superuser=True, is_active=True)
                await user.set_password(password)
                return await user.save()
        except (OperationalError, ProgrammingError) as exc:
            raise typer.BadParameter(f"Database error ({exc.orig}). Did you run `migrate`?") from None
        finally:
            await database.dispose()

    user = asyncio.run(create())
    _ok(f"Superuser {user.email} created (id={user.id}).")


@app.command()
def routes() -> None:
    """List the app's HTTP endpoints (from its OpenAPI schema) and websockets."""
    from fastapi_mvt.project import load_app

    application = load_app(_project())
    rows = []
    for path, item in application.openapi().get("paths", {}).items():
        for method, operation in item.items():
            rows.append((path, method.upper(), operation.get("summary", "")))
    for route in application.routes:
        if type(route).__name__.endswith("WebSocketRoute"):
            rows.append((route.path, "WS", ""))
    for path, method, summary in sorted(rows):
        typer.echo(f"{method:<7} {path:<45} {summary}")


@app.command()
def check() -> None:
    """Check the project: settings, app import, models and migrations. Exit 1 on problems (CI)."""
    from fastapi_mvt.migrations import commands
    from fastapi_mvt.project import load_app, load_project_models

    project = _project()
    problems: list[str] = []
    modules = load_project_models(project)
    typer.echo(f"Models: {', '.join(modules) or '(none found)'}")
    load_app(project)
    typer.echo(f"App: {project.app} imports cleanly")

    if not (project.migrations_path / "versions").exists() or not any((project.migrations_path / "versions").glob("*.py")):
        problems.append("No migrations yet: run `makemigrations`.")
    else:
        states = commands.showmigrations(project)
        pending = [s.revision for s in states if not s.applied]
        if pending:
            problems.append(f"Unapplied migrations: {', '.join(pending)}. Run `migrate`.")
        elif commands.has_unmigrated_changes(project, against_history=False):
            problems.append("Models have changes without a migration. Run `makemigrations`.")

    if problems:
        for problem in problems:
            _warn(f"  ✗ {problem}" if _can_encode("✗") else f"  x {problem}")
        raise typer.Exit(1)
    _ok("System check identified no issues.")


def _can_encode(text: str) -> bool:
    try:
        text.encode(sys.stdout.encoding or "utf-8")
        return True
    except UnicodeEncodeError:
        return False


@app.command()
def version() -> None:
    """Show the fastapi-mvt version."""
    typer.echo(__version__)


def _load_custom_commands() -> None:
    """Add every app's ``commands.py`` functions (see fastapi_mvt.commands) to the CLI."""
    import importlib

    from fastapi_mvt import commands as custom
    from fastapi_mvt.project import activate, discover_modules, find_project

    try:
        project = find_project()
    except Exception:
        return
    if project is None:
        return
    activate(project)
    for module in discover_modules(project.root, "commands"):
        try:
            importlib.import_module(module)
        except Exception as exc:  # a broken custom command must not block migrate & co.
            _warn(f"Could not load {module}: {type(exc).__name__}: {exc}")
    builtin = {c.name or (c.callback.__name__ if c.callback else "") for c in app.registered_commands}
    for registered in custom.registered():
        if registered.name in builtin:
            _warn(f"Custom command {registered.name!r} clashes with a built-in command and was skipped.")
            continue
        app.command(name=registered.name)(custom.as_cli_callable(registered.func))
        builtin.add(registered.name)


def main() -> None:
    """Entry point with readable errors instead of tracebacks for expected problems."""
    from fastapi_mvt.migrations.autogen import MigrationAborted
    from fastapi_mvt.migrations.commands import MigrationError
    from fastapi_mvt.project import ModelImportError, ProjectError
    from fastapi_mvt.scaffold import ScaffoldError

    _load_custom_commands()
    try:
        code = app(standalone_mode=False)
        if isinstance(code, int) and code:
            sys.exit(code)
    except (ProjectError, MigrationError, MigrationAborted, ScaffoldError) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(1)
    except ModelImportError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        typer.secho("\nOriginal traceback:", err=True)
        import traceback

        traceback.print_exception(exc.__cause__, file=sys.stderr)
        sys.exit(1)
    except typer.Exit as exc:
        sys.exit(exc.exit_code)
    except typer.Abort:
        typer.echo("Aborted.", err=True)
        sys.exit(1)
    except Exception as exc:
        import click

        if isinstance(exc, click.ClickException):
            exc.show()
            sys.exit(exc.exit_code)
        raise


if __name__ == "__main__":
    main()
