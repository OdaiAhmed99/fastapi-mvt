"""Custom management commands, like Django's ``management/commands``.

Put a ``commands.py`` in any app (or the project package)::

    # events/commands.py
    from fastapi_mvt.commands import command

    @command()
    async def close_past_events(dry_run: bool = False) -> None:
        \"\"\"Close events that have ended.\"\"\"
        events = Event.objects.filter(ends_at__lt=utcnow(), closed=False)
        print(f"{await events.count()} event(s) to close")
        if not dry_run:
            await events.update(closed=True)

    $ python manage.py close_past_events --dry-run

Parameters become options/arguments (via Typer), the docstring becomes the help
text, and async commands run in one database transaction.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class RegisteredCommand:
    name: str
    func: Callable[..., Any]


_registry: dict[str, RegisteredCommand] = {}


def command(name: Optional[str] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a function as ``python manage.py <name>`` (default: the function's name)."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        command_name = name or func.__name__
        _registry[command_name] = RegisteredCommand(command_name, func)
        return func

    return decorator


def registered() -> list[RegisteredCommand]:
    return list(_registry.values())


def as_cli_callable(func: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a command so it loads the project's models and, if async, runs in a transaction."""

    @functools.wraps(func)
    def run(*args: Any, **kwargs: Any) -> Any:
        from fastapi_mvt.project import get_project, load_database, load_project_models

        project = get_project()
        load_project_models(project)
        if inspect.iscoroutinefunction(func):
            database = load_database(project)

            async def main() -> Any:
                try:
                    async with database.session():
                        return await func(*args, **kwargs)
                finally:
                    await database.dispose()

            return asyncio.run(main())
        return func(*args, **kwargs)

    return run
