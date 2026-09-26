"""Alembic integration.

A project's ``migrations/env.py`` is two lines that call :func:`run_migrations`,
so every project migrates the same, tested way:

* all models are imported, and a broken models module stops the run
* the URL comes from the project's ``Database`` (async drivers work)
* SQLite gets batch mode, and each migration runs in its own transaction
"""

from __future__ import annotations

import asyncio
from typing import Any


def _include_object(obj: Any, name: str, type_: str, reflected: bool, compare_to: Any) -> bool:
    if type_ == "table" and name == "alembic_version":
        return False
    if getattr(obj, "info", {}).get("skip_migrations"):
        return False
    return True


def _render_item(type_: str, obj: Any, autogen_context: Any) -> Any:
    from fastapi_mvt.db.models import UTCDateTime

    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations() -> None:
    """Entry point for ``migrations/env.py``."""
    from alembic import context

    from fastapi_mvt.db.models import Base
    from fastapi_mvt.project import get_project, load_database, load_project_models

    config = context.config
    project = config.attributes.get("project") or get_project()
    database = config.attributes.get("database") or load_database(project)
    load_project_models(project)

    options: dict[str, Any] = dict(
        target_metadata=Base.metadata,
        compare_type=True,
        render_as_batch=database.is_sqlite,
        include_object=_include_object,
        render_item=_render_item,
        transaction_per_migration=True,
    )
    for key in ("process_revision_directives", "on_version_apply"):
        if config.attributes.get(key) is not None:
            options[key] = config.attributes[key]

    if context.is_offline_mode():
        sync_url = database.url.set(drivername=database.url.get_backend_name())
        context.configure(
            url=sync_url.render_as_string(hide_password=False),
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
            **options,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    def run(connection: Any) -> None:
        context.configure(connection=connection, **options)
        with context.begin_transaction():
            context.run_migrations()

    connection = config.attributes.get("connection")
    if connection is not None:
        run(connection)
        return

    async def run_async() -> None:
        try:
            async with database.engine.connect() as conn:
                await conn.run_sync(run)
                await conn.commit()
        finally:
            await database.dispose()

    asyncio.run(run_async())
