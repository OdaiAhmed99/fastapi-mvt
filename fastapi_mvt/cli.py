"""
CLI commands for fastapi-mvt framework
Provides startproject and startapp commands
"""

import os
import shutil
import sys
import ast
import importlib
from pathlib import Path
from typing import Optional, Any, List
import datetime
import textwrap
import typer

app = typer.Typer(help="FastAPI MVT Framework CLI")


def _success_marker() -> str:
    encoding = getattr(sys.stdout, "encoding", None)
    if encoding:
        try:
            "✓".encode(encoding)
            return "✓"
        except UnicodeEncodeError:
            pass
    return "[OK]"


def create_directory(path: Path):
    """Create directory if it doesn't exist"""
    path.mkdir(parents=True, exist_ok=True)


def write_file(path: Path, content: str):
    """Write content to file"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def _scan_migration_files(migrations_dir: Path) -> List[Path]:
    if not migrations_dir.exists():
        return []
    return sorted([p for p in migrations_dir.iterdir() if p.suffix == ".py" and p.name != "__init__.py"])


def _is_noop_upgrade_migration(migration_file: Path) -> bool:
    """Return True when upgrade() contains no real Alembic operations."""
    try:
        source = migration_file.read_text(encoding="utf-8")
        module = ast.parse(source)
    except Exception:
        return False

    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            for n in node.body:
                # pass → skip
                if isinstance(n, ast.Pass):
                    continue
                # bare string literal (docstring) → skip
                if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
                    continue
                # anything else (op.create_table(), op.add_column(), …) → real migration
                return False
            return True  # nothing real found

    return False


def _require_alembic():
    try:
        from alembic.config import Config  # noqa: F401
        from alembic import command  # noqa: F401
    except Exception as exc:
        typer.secho(
            "Alembic is required for migrations. Install with: pip install alembic",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from exc


def _load_project_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path.cwd() / ".env")
    except ImportError:
        pass


def _detect_project_package(project_root: Path) -> str:
    for child in project_root.iterdir():
        if child.is_dir() and (child / "settings.py").exists():
            return child.name
    return project_root.name


def _ensure_alembic_scaffold(project_root: Path) -> None:
    alembic_ini = project_root / "alembic.ini"
    migrations_dir = project_root / "migrations"
    versions_dir = migrations_dir / "versions"
    env_py = migrations_dir / "env.py"
    script_mako = migrations_dir / "script.py.mako"

    if not alembic_ini.exists():
        alembic_ini.write_text(
            """[alembic]
script_location = migrations
prepend_sys_path = .
sqlalchemy.url = sqlite:///./app.db
""",
            encoding="utf-8",
        )

    versions_dir.mkdir(parents=True, exist_ok=True)

    project_package = _detect_project_package(project_root)

    expected_env_py = textwrap.dedent(
        """
        import os
        import sys
        import importlib
        from alembic import context
        from sqlalchemy import engine_from_config, pool
        from sqlalchemy.sql.naming import conv

        try:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(os.getcwd(), ".env"))
        except Exception:
            pass

        sys.path.insert(0, os.getcwd())

        config = context.config

        database_url = os.getenv("DATABASE_URL", "sqlite:///./app.db")
        config.set_main_option("sqlalchemy.url", database_url)

        # ── Discover project package and INSTALLED_APPS ───────────────────────
        project_package = "__PROJECT_PACKAGE__"
        try:
            settings_mod = importlib.import_module(f"{project_package}.settings")
            installed_apps = settings_mod.Settings().INSTALLED_APPS
        except Exception:
            installed_apps = []

        # ── Import project main.py so configure_auth() and any startup
        #    imports fire, registering all models against their Base. ──────────
        try:
            importlib.import_module(f"{project_package}.main")
        except Exception:
            pass

        # ── Import every app's models module ──────────────────────────────────
        for app_name in installed_apps:
            try:
                importlib.import_module(f"{app_name}.models")
            except Exception:
                pass

        # ── Use the shared Base.metadata directly ─────────────────────────────
        # All models (project + fastapi_mvt library) register against the same
        # Base from fastapi_mvt.db, which carries naming_convention.
        # Using Base.metadata directly preserves constraint names — the old
        # to_metadata() merge approach silently stripped them.
        from fastapi_mvt.db.base import Base as _LibBase
        target_metadata = _LibBase.metadata

        uses_custom_user_table = "users" in target_metadata.tables


        def include_object(object_, name, type_, reflected, compare_to):
            if type_ == "table" and name in {"schema_migrations", "alembic_version"}:
                return False
            if type_ == "table" and name == "auth_users" and uses_custom_user_table:
                return False
            return True


        def _is_sqlite(url_or_conn) -> bool:
            s = str(url_or_conn)
            return s.startswith("sqlite")


        def _fix_constraint_names(migration_context, revision, directives):
            \"\"\"
            Alembic autogenerate renders SQLAlchemy conv-typed constraint names
            as None instead of the resolved string when using batch mode on SQLite.
            This hook walks every generated op and replaces any None constraint
            name with the string value from the conv object on the constraint.
            \"\"\"
            for migration in directives:
                upgrade_ops_list = (
                    migration.upgrade_ops_list
                    if hasattr(migration, "upgrade_ops_list")
                    else [migration.upgrade_ops]
                )
                for upgrade_ops in upgrade_ops_list:
                    if upgrade_ops is None:
                        continue
                    for op in upgrade_ops.ops:
                        _fix_op(op)


        def _fix_op(op):
            if hasattr(op, "ops"):
                for child in op.ops:
                    _fix_op(child)
            if hasattr(op, "constraint_name") and op.constraint_name is None:
                constraint = getattr(op, "constraint", None) or getattr(op, "_constraint", None)
                if constraint is not None and isinstance(getattr(constraint, "name", None), conv):
                    op.constraint_name = str(constraint.name)


        def run_migrations_offline() -> None:
            url = config.get_main_option("sqlalchemy.url")
            context.configure(
                url=url,
                target_metadata=target_metadata,
                literal_binds=True,
                compare_type=True,
                compare_server_default=True,
                include_object=include_object,
                render_as_batch=_is_sqlite(url),
                process_revision_directives=_fix_constraint_names,
            )

            with context.begin_transaction():
                context.run_migrations()


        def run_migrations_online() -> None:
            connectable = engine_from_config(
                config.get_section(config.config_ini_section) or {},
                prefix="sqlalchemy.",
                poolclass=pool.NullPool,
            )

            with connectable.connect() as connection:
                context.configure(
                    connection=connection,
                    target_metadata=target_metadata,
                    compare_type=True,
                    compare_server_default=True,
                    include_object=include_object,
                    render_as_batch=_is_sqlite(connectable.url),
                    process_revision_directives=_fix_constraint_names,
                )

                with context.begin_transaction():
                    context.run_migrations()


        if context.is_offline_mode():
            run_migrations_offline()
        else:
            run_migrations_online()
        """
    ).strip().replace("__PROJECT_PACKAGE__", project_package) + "\n"

    current_env_py = env_py.read_text(encoding="utf-8") if env_py.exists() else ""
    if current_env_py != expected_env_py:
        env_py.write_text(expected_env_py, encoding="utf-8")

    expected_script_mako = textwrap.dedent(
        '''
        """${message}

        Revision ID: ${up_revision}
        Revises: ${down_revision | comma,n}
        Create Date: ${create_date}

        """
        from alembic import op
        import sqlalchemy as sa


        revision = ${repr(up_revision)}
        down_revision = ${repr(down_revision)}
        branch_labels = ${repr(branch_labels)}
        depends_on = ${repr(depends_on)}


        def upgrade() -> None:
        ${upgrades if upgrades else "    pass"}


        def downgrade() -> None:
        ${downgrades if downgrades else "    pass"}
        '''
    ).strip() + "\n"

    # Self-heal malformed script templates that may have been generated by old versions.
    current_template = script_mako.read_text(encoding="utf-8") if script_mako.exists() else ""
    if current_template != expected_script_mako:
        script_mako.write_text(expected_script_mako, encoding="utf-8")


def _get_alembic_config(project_root: Path):
    _require_alembic()
    from alembic.config import Config

    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", os.getenv("DATABASE_URL", "sqlite:///./app.db"))
    return cfg


@app.command()
def makemigrations(
    app_name: Optional[str] = typer.Argument(None, help="App to create migrations for (default: all INSTALLED_APPS)"),
    name: Optional[str] = typer.Option(None, help="Short name/description for the migration file")
):
    """Create an Alembic migration revision using autogenerate."""
    _load_project_env()
    project_root = Path.cwd()
    _ensure_alembic_scaffold(project_root)
    cfg = _get_alembic_config(project_root)

    # Build message: <app>_<name> or <app>_001, <app>_002 …
    if app_name:
        if name:
            label = name.strip().replace(" ", "_")
            message = f"{app_name}_{label}"
        else:
            # Count existing revisions for this app to get the next number
            versions_dir = project_root / "migrations" / "versions"
            existing = list(versions_dir.glob(f"*_{app_name}_*.py")) + list(versions_dir.glob(f"*_{app_name}.py"))
            seq = len(existing) + 1
            message = f"{app_name}_{seq:03d}"
    else:
        message = (name or "auto").strip().replace(" ", "_")

    from alembic import command
    from alembic.util.exc import CommandError

    versions_dir = project_root / "migrations" / "versions"
    before = set(versions_dir.glob("*.py")) if versions_dir.exists() else set()

    # Autogenerate requires the DB to be at the current head.
    # Silently upgrade first so repeated `makemigrations` calls work without
    # requiring the user to run `migrate` in between.
    try:
        command.upgrade(cfg, "head")
    except Exception:
        pass  # DB may not exist yet on first run; alembic will create it

    import io
    import contextlib

    # Suppress Alembic's own stdout so we control the output ourselves.
    _buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(_buf):
            command.revision(cfg, message=message, autogenerate=True)
    except CommandError as exc:
        if "not up to date" in str(exc).lower():
            typer.secho(
                "Database is not up to date. Run `migrate` first, then try again.",
                fg=typer.colors.RED,
            )
            raise typer.Exit(1)
        raise

    # Find the newly created file
    after = set(versions_dir.glob("*.py"))
    new_files = after - before

    if new_files:
        new_file = max(new_files, key=lambda f: f.stat().st_mtime)
        if _is_noop_upgrade_migration(new_file):
            new_file.unlink()
            typer.secho("No changes detected — migration file not created.", fg=typer.colors.YELLOW)
            return
        # Real migration — show the file path Alembic reported
        typer.echo(_buf.getvalue().strip())

    typer.echo(f"{_success_marker()} Alembic migration generated in migrations/versions")


@app.command()
def migrate(
    app_name: Optional[str] = typer.Option(None, help="Run migrations for a single app (default: all INSTALLED_APPS)"),
    revision: str = typer.Option("head", help="Alembic revision target, e.g. head, +1, base, <revision_id>"),
    schema_sync: bool = typer.Option(False, "--schema-sync", help="Deprecated: ignored in Alembic mode")
):
    """Apply Alembic migrations (upgrade)."""
    _load_project_env()
    project_root = Path.cwd()
    _ensure_alembic_scaffold(project_root)
    cfg = _get_alembic_config(project_root)

    if app_name:
        typer.secho(
            "Note: app_name is ignored in Alembic mode; migrate is project-wide.",
            fg=typer.colors.YELLOW,
        )
    if schema_sync:
        typer.secho(
            "Note: --schema-sync is deprecated and ignored in Alembic mode.",
            fg=typer.colors.YELLOW,
        )

    from alembic import command

    typer.echo("Operations to perform:")
    typer.echo(f"  Alembic upgrade to {revision}")
    command.upgrade(cfg, revision)
    typer.echo(f"{_success_marker()} Migration complete")


@app.command()
def checkmigrations(
    app_name: Optional[str] = typer.Option(None, help="Check migrations for a single app (default: all INSTALLED_APPS)")
):
    """Check Alembic version scripts for empty/no-op upgrade functions."""
    _load_project_env()
    project_root = Path.cwd()
    _ensure_alembic_scaffold(project_root)

    if app_name:
        typer.secho(
            "Note: app_name is ignored in Alembic mode; checks are project-wide.",
            fg=typer.colors.YELLOW,
        )

    versions_dir = project_root / "migrations" / "versions"
    files = _scan_migration_files(versions_dir)
    if not files:
        typer.echo("No Alembic version files found.")
        return

    empty_files = [f.name for f in files if _is_noop_upgrade_migration(f)]

    typer.echo("Migration check report:")
    typer.echo(f"- versions files: {len(files)}")
    typer.echo(f"- empty/no-op revisions: {len(empty_files)}")

    for name in empty_files:
        typer.secho(f"    * {name}", fg=typer.colors.YELLOW)

    if not empty_files:
        typer.secho("No empty Alembic revisions found.", fg=typer.colors.GREEN)


@app.command()
def startproject(
    name: str = typer.Argument(..., help="Project name"),
    directory: Optional[str] = typer.Option(None, help="Target directory")
):
    """Scaffold a new FastAPI MVT project with the standard directory layout."""
    # Determine project path
    if directory:
        project_path = Path(directory) / name
    else:
        project_path = Path.cwd() / name
    
    if project_path.exists():
        typer.echo(f"Error: Directory {project_path} already exists", err=True)
        raise typer.Exit(1)
    
    typer.echo(f"Creating project: {name}")
    
    # Create project structure
    create_directory(project_path)
    create_directory(project_path / name)
    
    # Create manage.py
    manage_py = f'''#!/usr/bin/env python
"""
Management script for {name}.

Usage
-----
    python manage.py                              # show available commands
    python manage.py runserver                    # start the dev server
    python manage.py makemigrations [app]         # create migration file(s)
    python manage.py migrate [--app-name app]     # apply pending migrations
    python manage.py startapp <name>              # scaffold a new app
    python manage.py startproject <name>          # scaffold a new project
    python manage.py createsuperuser              # create an admin user
    python manage.py worker                       # start a Celery worker
    python manage.py beat                         # start the Celery Beat scheduler
    python manage.py worker --beat                # worker + beat combined (dev only)
    python manage.py flower                       # start Flower monitoring UI

Framework commands are delegated to `fastapi-mvt <command>`. The `runserver`,
`worker`, `beat`, and `flower` shortcuts are project-local.
"""

import sys


COMMAND_HELP = """FastAPI MVT management commands

Usage:
    python manage.py <command> [options]

Commands:
    runserver                       Start the development server
    makemigrations [app]            Create migration file(s)
    migrate [--app-name app]        Apply pending migrations
    startapp <name>                 Scaffold a new app
    startproject <name>             Scaffold a new project
    createsuperuser                 Create an admin user
    worker [--beat]                 Start a Celery worker (--beat adds the scheduler)
    beat                            Start the Celery Beat scheduler (standalone)
    flower [--port PORT]            Start Flower, the Celery monitoring UI

Use "python manage.py <command> --help" for command-specific options.
"""


def main():
    args = sys.argv[1:]

    if not args or args[0] in ("help", "--help", "-h"):
        print(COMMAND_HELP)
        return

    # Explicit \'runserver\' starts uvicorn.
    if args[0] == "runserver":
        import uvicorn
        uvicorn.run("{name}.main:app", host="0.0.0.0", port=8000, reload=True)
        return

    # Celery worker
    if args[0] == "worker":
        import subprocess
        beat_flag = ["--beat"] if "--beat" in args[1:] else []
        subprocess.run(
            [sys.executable, "-m", "celery", "-A", "{name}.celery", "worker",
             "--loglevel=info"] + beat_flag
        )
        return

    # Celery Beat scheduler (standalone)
    if args[0] == "beat":
        import subprocess
        subprocess.run(
            [sys.executable, "-m", "celery", "-A", "{name}.celery", "beat",
             "--loglevel=info"]
        )
        return

    # Flower monitoring UI  (pip install flower)
    if args[0] == "flower":
        import subprocess
        port = "5555"
        for a in args[1:]:
            if a.startswith("--port="):
                port = a.split("=", 1)[1]
            elif a == "--port" and args.index(a) + 1 < len(args):
                port = args[args.index(a) + 1]
        subprocess.run(
            [sys.executable, "-m", "celery", "-A", "{name}.celery",
             "flower", f"--port={{port}}"]
        )
        return

    # Everything else delegates to the fastapi_mvt CLI.
    # Typer reads sys.argv[1:], so just call app() directly.
    from fastapi_mvt.cli import app
    app()


if __name__ == "__main__":
    main()
'''
    write_file(project_path / "manage.py", manage_py)
    
    # Create .env
    env_file = f'''# {name} Environment Configuration

# Database
DATABASE_URL=sqlite:///./app.db

# Debug mode
DEBUG=True

# Secret key — CHANGE THIS before deploying to production!
SECRET_KEY=your-secret-key-change-this-in-production

# CORS Settings
CORS_ENABLED=True
CORS_ORIGINS=["http://localhost:3000","http://localhost:8080"]
CORS_ALLOW_CREDENTIALS=True

# Security Settings
CSRF_ENABLED=False
SECURITY_HEADERS_ENABLED=True

# Celery
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0
'''
    write_file(project_path / ".env", env_file)
    
    # Create main.py
    main_py = f'''"""
{name} - FastAPI REST API
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from {name}.settings import settings
from {name}.registry import registry

# ── Auth (built-in, fully hidden) ─────────────────────────────────────────────
# The library provides /auth/register, /auth/login, /auth/logout,
# /auth/refresh, and /auth/me out of the box.
# Developers never touch an auth/ app — just configure here and go.
from fastapi_mvt.auth import configure_auth, AuthConfig, auth_router
from {name}.db import get_db, Base

configure_auth(AuthConfig(
    secret_key=settings.SECRET_KEY,
    # To use a custom user model, uncomment and point to your own class:
    # user_model="accounts.models.User",
    db_dependency=get_db,
))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────────────────
    # Import signals.py from every installed app and wire SQLAlchemy ORM events.
    # After this, @receiver decorators fire automatically on every save/delete.
    from fastapi_mvt.core.signals import setup_signals
    setup_signals(settings.INSTALLED_APPS, Base)
    yield
    # ── Shutdown ──────────────────────────────────────────────────────────────


# Create FastAPI app
app = FastAPI(
    title=settings.PROJECT_NAME,
    debug=settings.DEBUG,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── Built-in auth endpoints (/auth/register /auth/login /auth/logout …) ──────
app.include_router(auth_router)

# Auto-discover and register apps (reads urls.py router from each INSTALLED_APP)
registry.autodiscover(settings.INSTALLED_APPS)
registry.setup_app(app)


@app.get("/", tags=["Root"])
async def root():
    return {{
        "message": "Welcome to {name} API",
        "docs": "/docs",
        "health": "/health",
    }}


@app.get("/health", tags=["Root"])
async def health():
    return {{
        "status": "ok",
        "project": "{name}",
    }}


# ── Setup middleware ──────────────────────────────────────────────────────────
from {name}.middleware import setup_middleware
setup_middleware(app, settings)
'''
    write_file(project_path / name / "main.py", main_py)
    
    # Create settings.py
    settings_py = f'''"""
{name} settings configuration
"""

import os
from typing import List, Optional
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


class Settings(BaseSettings):
    """Application settings"""
    
    # Project
    PROJECT_NAME: str = "{name}"
    DEBUG: bool = os.getenv("DEBUG", "True").lower() == "true"
    SECRET_KEY: str = os.getenv("SECRET_KEY", "change-this-secret-key")
    
    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./app.db")
    
    # Authentication is built into fastapi-mvt — no extra settings needed here.
    # Configure it in main.py via AuthConfig. The only value you must supply
    # is SECRET_KEY (already defined above).
    
    # CORS Settings
    CORS_ENABLED: bool = os.getenv("CORS_ENABLED", "True").lower() == "true"
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:8080"]
    CORS_ALLOW_CREDENTIALS: bool = os.getenv("CORS_ALLOW_CREDENTIALS", "True").lower() == "true"
    CORS_ALLOW_METHODS: List[str] = ["*"]
    CORS_ALLOW_HEADERS: List[str] = ["*"]
    
    # Security
    CSRF_ENABLED: bool = os.getenv("CSRF_ENABLED", "False").lower() == "true"
    SECURITY_HEADERS_ENABLED: bool = os.getenv("SECURITY_HEADERS_ENABLED", "True").lower() == "true"
    
    # Celery
    CELERY_BROKER_URL: str = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    CELERY_RESULT_BACKEND: str = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
    
    # Installed apps
    INSTALLED_APPS: List[str] = [
        # Add your business apps here, e.g., "blog", "shop"
        # Auth is built into the library — no auth app needed here.
    ]
    
    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
'''
    write_file(project_path / name / "settings.py", settings_py)
    
    # Create db.py
    db_py = f'''"""
Database configuration for {name}

Provides both a sync session (``get_db``) and an async session
(``get_async_db``) so you can use either style in your endpoints.
"""

from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from fastapi_mvt.db import (
    Base,
    get_engine, get_session_local, get_db as _get_db,
    get_async_engine, get_async_session_local, get_async_db as _get_async_db,
    create_tables,
)
from {name}.settings import settings

# ── Sync engine & session ────────────────────────────────────────────────────
engine = get_engine(settings.DATABASE_URL, echo=settings.DEBUG)
SessionLocal = get_session_local(engine)


def get_db() -> Session:
    """Sync database session — use in regular (non-async) endpoints."""
    yield from _get_db(SessionLocal)


# ── Async engine & session ───────────────────────────────────────────────────
async_engine = get_async_engine(settings.DATABASE_URL, echo=settings.DEBUG)
AsyncSessionLocal = get_async_session_local(async_engine)


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    """Async database session — use in ``async def`` endpoints.

    Example::

        from sqlalchemy import select
        from {name}.db import get_async_db
        from auth.models import User

        @router.get("/users")
        async def list_users(db: AsyncSession = Depends(get_async_db)):
            result = await db.execute(select(User))
            return result.scalars().all()
    """
    async for session in _get_async_db(AsyncSessionLocal):
        yield session
'''
    write_file(project_path / name / "db.py", db_py)
    
    # Create registry.py
    registry_py = f'''"""
App registry for {name}
"""

from fastapi_mvt.core.registry import AppRegistry

# Create global registry instance
registry = AppRegistry()
'''
    write_file(project_path / name / "registry.py", registry_py)

    # No project-level auth.py needed — auth is fully provided by the library.
    # Developers use `from fastapi_mvt.auth import get_current_user` directly.

    # Create websocket.py
    websocket_py = f'''"""
Project-level WebSocket manager. Import `manager` in your route handlers to connect clients and push events.
Supports in-memory (default), Redis (multi-worker), or any custom broker backend. See the online docs.
"""

from fastapi_mvt.utils.websocket import ConnectionManager

manager = ConnectionManager()

__all__ = ["manager", "ConnectionManager"]
'''
    write_file(project_path / name / "websocket.py", websocket_py)

    # Create middleware.py
    middleware_py = f'''"""  
Middleware configuration for {name}

Uses fastapi_mvt middleware utilities for security.
Customize by extending middleware classes or adding your own.
"""

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from typing import Callable
from fastapi_mvt.middleware import (
    SecurityHeadersMiddleware,
    SQLInjectionProtectionMiddleware,
    RateLimitMiddleware,
    CustomMiddleware as BaseCustomMiddleware,
    setup_cors_middleware,
    setup_security_middleware,
)


class CustomMiddleware(BaseCustomMiddleware):
    """
    Custom middleware for your application
    
    Override dispatch() to add custom logic:
    - Request logging
    - Performance monitoring
    - Custom headers
    - Request/response modification
    
    Example:
        async def dispatch(self, request: Request, call_next: Callable) -> Response:
            # Before request
            start_time = time.time()
            
            response = await call_next(request)
            
            # After request
            process_time = time.time() - start_time
            response.headers["X-Process-Time"] = str(process_time)
            
            return response
    """
    pass


def setup_middleware(app, settings):
    """
    Setup all middleware for the application
    
    Args:
        app: FastAPI application instance
        settings: Application settings
    
    Middleware order matters! They are executed in the order added.
    """
    # Session middleware (required for CSRF)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.SECRET_KEY,
        max_age=3600 * 24 * 7  # 7 days
    )
    
    # CORS middleware (if enabled)
    if settings.CORS_ENABLED:
        setup_cors_middleware(
            app,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
            allow_methods=settings.CORS_ALLOW_METHODS,
            allow_headers=settings.CORS_ALLOW_HEADERS,
        )
    
    # Security middleware from library
    setup_security_middleware(
        app,
        enable_security_headers=settings.SECURITY_HEADERS_ENABLED,
        enable_sql_injection_protection=True,  # Always enabled by default
        enable_rate_limiting=True,
        rate_limit_requests_per_minute=60,
    )
    
    # Custom middleware (add your own)
    app.add_middleware(CustomMiddleware)
'''
    write_file(project_path / name / "middleware.py", middleware_py)
    
    # Create celery.py
    celery_py = f'''"""
{name}.celery
{'=' * (len(name) + 7)}

Project-level Celery application.

Celery requires a *broker* (sends/receives task messages) and a
*result backend* (stores return values). Both are configured via
environment variables so you never hard-code connection strings.

─────────────────────────────────────────────
BROKER OPTIONS
─────────────────────────────────────────────
Set CELERY_BROKER_URL in your .env file.

  Redis (default, recommended):
      CELERY_BROKER_URL=redis://localhost:6379/0

  RabbitMQ:
      CELERY_BROKER_URL=amqp://guest:guest@localhost:5672//

  In-memory (testing only — tasks run in the same process, no worker needed):
      CELERY_BROKER_URL=memory://
      CELERY_RESULT_BACKEND=cache+memory://

─────────────────────────────────────────────
RESULT BACKEND OPTIONS
─────────────────────────────────────────────
Set CELERY_RESULT_BACKEND in your .env file.

  Redis (default):
      CELERY_RESULT_BACKEND=redis://localhost:6379/0

  Database (SQLAlchemy):
      pip install celery[sqlalchemy]
      CELERY_RESULT_BACKEND=db+postgresql://user:pass@localhost/dbname

  Disable results (fire-and-forget):
      CELERY_RESULT_BACKEND=  # leave empty / comment out

─────────────────────────────────────────────
RUNNING THE WORKER
─────────────────────────────────────────────
  # development (1 process, verbose)
  celery -A {name}.celery worker --loglevel=info

  # production (concurrency = CPU cores)
  celery -A {name}.celery worker --loglevel=info --concurrency=4

  # periodic tasks (beat scheduler)
  celery -A {name}.celery beat --loglevel=info

  # combined worker + beat (dev only)
  celery -A {name}.celery worker --beat --loglevel=info

─────────────────────────────────────────────
DEFINING TASKS
─────────────────────────────────────────────
Create a tasks.py inside any app folder:

  # auth/tasks.py
  from {name}.celery import celery_app

  @celery_app.task(bind=True, max_retries=3)
  def send_welcome_email(self, user_id: int):
      try:
          user = get_user(user_id)
          send_email(user.email, subject="Welcome!", ...)
      except Exception as exc:
          raise self.retry(exc=exc, countdown=60)   # retry in 60 s

─────────────────────────────────────────────
CALLING TASKS FROM FASTAPI
─────────────────────────────────────────────
  from auth.tasks import send_welcome_email

  # fire and forget
  send_welcome_email.delay(user.id)

  # schedule for later
  from datetime import timedelta
  send_welcome_email.apply_async(args=[user.id], countdown=30)

  # wait for result (blocks the request — avoid in production)
  result = send_welcome_email.apply_async(args=[user.id])
  value  = result.get(timeout=10)

─────────────────────────────────────────────
PERIODIC TASKS (beat schedule)
─────────────────────────────────────────────
Add entries to CELERYBEAT_SCHEDULE below.

  from celery.schedules import crontab

  celery_app.conf.beat_schedule = {{
      "cleanup-expired-tokens": {{
          "task": "auth.tasks.cleanup_expired_tokens",
          "schedule": crontab(hour=3, minute=0),   # every day at 03:00 UTC
      }},
      "refresh-cache-every-5-min": {{
          "task": "catalog.tasks.refresh_cache",
          "schedule": 300,                          # every 300 seconds
      }},
  }}
"""

from celery import Celery
from {name}.settings import settings

# ── App ────────────────────────────────────────────────────────────────────────
celery_app = Celery(
    "{name}",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

# ── Serialisation & timezone ───────────────────────────────────────────────────
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Acknowledge tasks only after they finish (safer, prevents data loss on crash)
    task_acks_late=True,
    # Reject tasks that have been prefetched but not yet started if the worker dies
    task_reject_on_worker_lost=True,
)

# ── Periodic tasks (Celery Beat) ───────────────────────────────────────────────
# Uncomment and fill in entries to enable scheduled tasks.
# Run the scheduler with:  python manage.py beat
#
# from celery.schedules import crontab
#
# celery_app.conf.beat_schedule = {{
#     "example-every-minute": {{
#         "task": "auth.tasks.example_task",
#         "schedule": 60,                        # every 60 seconds
#     }},
#     "example-daily-at-midnight": {{
#         "task": "auth.tasks.cleanup_expired_tokens",
#         "schedule": crontab(hour=0, minute=0),  # every day at 00:00 UTC
#     }},
# }}

# ── Auto-discover tasks ────────────────────────────────────────────────────────
# Celery will look for a tasks.py module inside every installed app.
celery_app.autodiscover_tasks(lambda: settings.INSTALLED_APPS)

__all__ = ["celery_app"]
'''
    write_file(project_path / name / "celery.py", celery_py)
    
    # Create __init__.py
    write_file(project_path / name / "__init__.py", "")

    # Auth is built into the library — no auth/ app is generated.
    # Developers extend the user model only when they need extra fields:
    #
    #   class User(Base, AbstractAuthUser):
    #       __tablename__ = "users"
    #       full_name = Column(String(120))
    #
    # Then point to it in main.py:  AuthConfig(user_model="accounts.models.User")

    # (auth app files skipped — auth is provided by the library)

    # Create requirements.txt
    requirements = '''fastapi>=0.104.0
uvicorn[standard]>=0.24.0
sqlalchemy>=2.0.0
pydantic>=2.0.0
pydantic-settings>=2.0.0
python-dotenv>=1.0.0
jinja2>=3.1.0
celery>=5.3.0
redis>=5.0.0
python-multipart>=0.0.6
python-jose[cryptography]>=3.3.0
bcrypt>=4.0.0
itsdangerous>=2.1.0
websockets>=12.0
aiosqlite>=0.19.0
'''
    write_file(project_path / "requirements.txt", requirements)
    
    # Create README.md
    readme = f'''# {name}

FastAPI MVT Project - REST API with Built-in Authentication

## Quick Start

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Configure environment (edit `.env` file):
   - Set `SECRET_KEY` for production
   - Configure `DATABASE_URL`

3. Apply migrations and create an admin account:
```bash
python manage.py migrate
python manage.py createsuperuser
```

4. Run the development server:
```bash
python manage.py runserver
```

5. Visit:
   - http://localhost:8000 - API root
   - http://localhost:8000/docs - Interactive API docs
   - http://localhost:8000/health - Health check

## Authentication (Built-in, Always Enabled)

Authentication is provided by the `fastapi-mvt` library and is always active.
There is **no** `auth/` folder in your project — it is completely hidden inside
the library. You get 5 endpoints for free:

| Method | URL               | Description                      |
|--------|-------------------|----------------------------------|
| POST   | /auth/register    | Create a new account             |
| POST   | /auth/login       | Get access + refresh tokens      |
| POST   | /auth/logout      | Clear auth cookies               |
| POST   | /auth/refresh     | Refresh an expired access token  |
| GET    | /auth/me          | Get the current user's profile   |

### Protect any endpoint

```python
from fastapi import Depends
from fastapi_mvt.auth import get_current_user

@router.get("/dashboard")
def dashboard(current_user = Depends(get_current_user)):
    return {{"email": current_user.email}}
```

### Custom User Model (optional)

If you need extra fields, create your own model that extends `AbstractAuthUser`:

```python
# accounts/models.py
from fastapi_mvt.auth import AbstractAuthUser
from {name}.db import Base

class User(Base, AbstractAuthUser):
    __tablename__ = "users"
    # add your own columns here
    phone = Column(String(20), nullable=True)
```

Then point to it in `main.py`:

```python
configure_auth(AuthConfig(
    secret_key=settings.SECRET_KEY,
    user_model="accounts.models.User",
    db_dependency=get_db,
))
```

## Security Features

- **JWT Authentication**: Secure token-based authentication
- **Password Hashing**: Bcrypt with salt
- **CORS**: Configurable cross-origin access
- **Security Headers**: X-Frame-Options, CSP, X-XSS-Protection, etc.
- **SQL Injection Protection**: Built-in via SQLAlchemy
- **Middleware**: Extensible security middleware

## Create an App

```bash
python manage.py startapp myapp
```

Add to `INSTALLED_APPS` in settings.py: `"myapp"`

## Run Celery Worker

```bash
celery -A {name}.celery worker --loglevel=info
```

## Project Structure

```
{name}/
├── {name}/
│   ├── main.py           # FastAPI app + auth wiring
│   ├── settings.py       # Configuration
│   ├── middleware.py     # Security middleware
│   ├── db.py             # Database setup
│   └── celery.py         # Task queue config
├── manage.py             # Management commands
├── requirements.txt      # Dependencies
└── .env                  # Environment variables
```

## Notes

- Authentication is **always enabled** — no auth/ app is generated
- Extend the user model with `AbstractAuthUser` only when you need extra fields
- All passwords are **automatically hashed** with bcrypt
'''
    write_file(project_path / "README.md", readme)
    
    typer.echo(f"Project '{name}' created successfully!")
    typer.echo(f"\nNext steps:")
    typer.echo(f"  cd {name}")
    typer.echo(f"  pip install -r requirements.txt")
    typer.echo(f"  python manage.py migrate              # apply initial migrations")
    typer.echo(f"  python manage.py createsuperuser      # create admin account")
    typer.echo(f"  python manage.py                      # show available commands")
    typer.echo(f"  python manage.py runserver            # start dev server")
    typer.echo(f"")
    typer.echo(f"Other commands:")
    typer.echo(f"  python manage.py makemigrations       # create migrations for all apps")
    typer.echo(f"  python manage.py makemigrations auth  # create migrations for one app")
    typer.echo(f"  python manage.py startapp <name>      # scaffold a new app")


@app.command()
def startapp(
    name: str = typer.Argument(..., help="App name"),
    directory: Optional[str] = typer.Option(None, help="Target directory (default: project root)")
):
    """Scaffold a new app inside the current project."""
    # Determine app path (default: project root, same level as manage.py)
    base_dir = Path.cwd() / directory if directory else Path.cwd()
    app_path = base_dir / name
    
    if not base_dir.exists():
        typer.echo(f"Error: Directory {base_dir} doesn't exist", err=True)
        typer.echo("Are you in the project root directory?", err=True)
        raise typer.Exit(1)

    # Create schemas.py
    schemas_py = f'''"""Pydantic request/response schemas for {name}. See the docs for validation and serialization patterns."""

from pydantic import BaseModel, ConfigDict
'''
    write_file(app_path / "schemas.py", schemas_py)
    
    # Create services.py
    services_py = f'''"""Business logic for {name}. Keep DB queries and rules here, call from views. See the docs."""

from sqlalchemy.orm import Session
'''
    write_file(app_path / "services.py", services_py)
    
    # Create views.py
    views_py = f'''"""FastAPI route handlers for {name}. Add endpoints to `router` and they are auto-mounted. See the docs."""

from fastapi import APIRouter, Depends
from fastapi_mvt.utils import get_db_session

get_db = get_db_session()
router = APIRouter()
'''
    write_file(app_path / "views.py", views_py)
    
    # Create urls.py
    urls_py = f'''"""{name} URL routing"""

from fastapi import APIRouter
from .views import router as views_router

# Create main router for this app
router = APIRouter(prefix="/{name}", tags=["{name}"])
router.include_router(views_router)
'''
    write_file(app_path / "urls.py", urls_py)
    
    # Create signals.py
    signals_py = f'''"""Signal handlers for {name}. Use @post_save.connect / @pre_delete.connect to react to model events. See the docs."""

from fastapi_mvt.core.signals import post_save, pre_delete
'''
    write_file(app_path / "signals.py", signals_py)
    
    # Create tasks.py
    tasks_py = f'''"""Celery background tasks for {name}. Decorate functions with @shared_task and call them with .delay(). See the docs."""

from celery import shared_task
'''
    write_file(app_path / "tasks.py", tasks_py)
    
    # Create tests.py
    tests_py = f'''"""Tests for {name}. Use TestClient to hit your endpoints. See the docs."""

from fastapi.testclient import TestClient
'''
    write_file(app_path / "tests.py", tests_py)

    # Create migrations/ folder at the same level as models.py, views.py, etc.
    migrations_path = app_path / "migrations"
    create_directory(migrations_path)
    write_file(migrations_path / "__init__.py", "# migrations package\n")

    typer.echo(f"App '{name}' created successfully!")
    typer.echo(f"\nNext steps:")
    typer.echo(f"  1. Add '{name}' to INSTALLED_APPS in settings.py")
    typer.echo(f"  2. Define your models in {name}/models.py")
    typer.echo(f"  3. Run: python manage.py makemigrations {name}")
    typer.echo(f"  4. Edit the generated migration file, then run: python manage.py migrate {name}")
    typer.echo(f"  5. Implement views, services, signals as needed")
    typer.echo(f"  6. Restart server and visit http://localhost:8000/docs")


@app.command()
def createsuperuser(
    email: Optional[str] = typer.Option(None, "--email", "-e", help="Superuser email"),
    password: Optional[str] = typer.Option(None, "--password", "-p", help="Superuser password (prompted if omitted)"),
):
    """Create a superuser. Reads the user model from AuthConfig in main.py."""
    import importlib
    import sys

    # ── Load project .env ────────────────────────────────────────────────────
    try:
        from dotenv import load_dotenv
        load_dotenv(Path.cwd() / ".env")
    except ImportError:
        pass

    database_url = os.getenv("DATABASE_URL", "sqlite:///./app.db")

    # ── Add project root to sys.path ─────────────────────────────────────────
    project_root = str(Path.cwd())
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # ── Prompt for missing values ─────────────────────────────────────────────
    if not email:
        email = typer.prompt("Email address")
    if not password:
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)

    if not email or not password:
        typer.echo("Error: email and password are required.", err=True)
        raise typer.Exit(1)

    # ── Import the project's main.py to trigger configure_auth() ─────────────
    # Discover project package: a subdirectory with __init__.py AND main.py
    project_pkg = None
    for item in sorted(Path.cwd().iterdir()):
        if (
            item.is_dir()
            and (item / "__init__.py").exists()
            and (item / "main.py").exists()
        ):
            project_pkg = item.name
            break

    if project_pkg:
        try:
            importlib.import_module(f"{project_pkg}.main")
        except Exception as e:
            typer.echo(
                f"Warning: could not import {project_pkg}.main ({e}).\n"
                "Falling back to DefaultAuthUser.",
                err=True,
            )

    # ── Resolve the user model via AuthConfig (same path as the router) ───────
    try:
        from fastapi_mvt.auth.config import get_auth_config, get_user_model
        get_auth_config()          # raises RuntimeError if configure_auth() was never called
        UserModel = get_user_model()
    except RuntimeError:
        # configure_auth() was never called — use the built-in default
        from fastapi_mvt.auth.models import DefaultAuthUser
        UserModel = DefaultAuthUser
    except Exception as e:
        typer.echo(f"Error resolving user model: {e}", err=True)
        raise typer.Exit(1)

    # ── Hash the password via the auth backend ───────────────────────────────
    try:
        from fastapi_mvt.auth.config import get_backend
        try:
            hashed = get_backend().hash_password(password)
        except RuntimeError:
            # No config yet — use JWTAuthBackend directly
            from fastapi_mvt.auth.backends import JWTAuthBackend
            hashed = JWTAuthBackend(secret_key="cli", algorithm="HS256").hash_password(password)
    except Exception as e:
        typer.echo(f"Error hashing password: {e}", err=True)
        raise typer.Exit(1)

    # ── Connect to the database ───────────────────────────────────────────────
    from fastapi_mvt.db.base import get_engine, get_session_local, Base
    import fastapi_mvt.auth.models as _auth_models  # noqa: F401 — register lib tables

    engine = get_engine(database_url)

    # Ensure all known tables exist (CREATE TABLE IF NOT EXISTS)
    Base.metadata.create_all(bind=engine, checkfirst=True)
    try:
        UserModel.__table__.create(bind=engine, checkfirst=True)
    except Exception:
        pass

    SessionLocal = get_session_local(engine)
    db = SessionLocal()

    try:
        # ── Check for duplicate email ─────────────────────────────────────────
        existing = db.query(UserModel).filter(UserModel.email == email).first()
        if existing:
            typer.echo(f"Error: a user with email '{email}' already exists.", err=True)
            raise typer.Exit(1)

        # ── Insert the superuser ──────────────────────────────────────────────
        user = UserModel(
            email=email,
            hashed_password=hashed,
            is_superuser=True,
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        typer.echo(f"Superuser '{email}' created successfully (id={user.id}).")

    except typer.Exit:
        raise
    except Exception as e:
        db.rollback()
        typer.echo(f"Error creating superuser: {e}", err=True)
        raise typer.Exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    app()
