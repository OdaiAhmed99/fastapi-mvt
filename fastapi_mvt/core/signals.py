"""
Production-ready Django-inspired signal system for FastAPI MVT.

Signals fire automatically via SQLAlchemy ORM events — no manual .send()
calls are needed inside views or services.

Quick start::

    from fastapi_mvt.signals import receiver, post_save
    from myapp.models import Event

    @receiver(post_save, sender=Event)
    async def on_event_saved(sender, instance, created, **kwargs):
        if created:
            print(f"New event: {instance.id}")

Then in startup::

    from fastapi_mvt.signals import register_all_model_signals
    from myproject.db import Base
    register_all_model_signals(Base)
"""

import asyncio
import importlib
import logging
from typing import Any, Callable, List, Optional, Sequence, Tuple

from sqlalchemy import event as sa_event

logger = logging.getLogger(__name__)

# Tracks models that already have SQLAlchemy listeners attached.
_registered_models: set = set()


# ---------------------------------------------------------------------------
# Core Signal class
# ---------------------------------------------------------------------------

class Signal:
    """
    A signal that can have multiple sync or async receivers.

    Public API::

        signal.connect(callback, sender=None)
        signal.disconnect(callback, sender=None)
        signal.send(sender, **kwargs)           # sync dispatch
        await signal.asend(sender, **kwargs)    # async dispatch
    """

    def __init__(self, name: str = ""):
        self.name = name
        # Each entry is (callback, sender_filter). sender_filter=None means all.
        self._receivers: List[Tuple[Callable, Any]] = []

    # -- Registration --------------------------------------------------------

    def connect(self, callback: Callable, sender: Any = None) -> Callable:
        """Connect *callback*. Duplicate (callback, sender) pairs are ignored."""
        if (callback, sender) not in self._receivers:
            self._receivers.append((callback, sender))
        return callback

    def disconnect(self, callback: Callable, sender: Any = None) -> None:
        """
        Disconnect *callback*.  If *sender* is None, all registrations for
        that callback are removed regardless of their sender filter.
        """
        self._receivers = [
            (cb, s) for cb, s in self._receivers
            if not (
                cb is callback
                and (sender is None or s is sender or s == sender)
            )
        ]

    # -- Helpers -------------------------------------------------------------

    def _matching(self, sender: Any) -> List[Callable]:
        return [
            cb for cb, s in self._receivers
            if s is None or s is sender or s == sender
        ]

    # -- Dispatch ------------------------------------------------------------

    def send(self, sender: Any = None, **kwargs) -> List[Tuple[Callable, Any]]:
        """
        Synchronous dispatch.

        * Sync receivers are called immediately.
        * Async receivers are scheduled on the running event loop (if any) or
          executed via ``asyncio.run()`` as a fallback.

        Returns a Django-style list of ``(callback, result)`` pairs.
        """
        responses: List[Tuple[Callable, Any]] = []
        for cb in self._matching(sender):
            if asyncio.iscoroutinefunction(cb):
                _schedule_async(cb, sender=sender, **kwargs)
                responses.append((cb, None))
            else:
                try:
                    result = cb(sender=sender, **kwargs)
                    responses.append((cb, result))
                except Exception:
                    logger.exception(
                        "Error in sync receiver %s for signal '%s'",
                        cb.__qualname__, self.name,
                    )
        return responses

    async def asend(
        self, sender: Any = None, **kwargs
    ) -> List[Tuple[Callable, Any]]:
        """
        Async dispatch — awaits async receivers, calls sync ones normally.

        Use this from ``async def`` route handlers or background tasks when
        you want to await results before continuing.
        """
        responses: List[Tuple[Callable, Any]] = []
        for cb in self._matching(sender):
            try:
                if asyncio.iscoroutinefunction(cb):
                    result = await cb(sender=sender, **kwargs)
                else:
                    result = cb(sender=sender, **kwargs)
                responses.append((cb, result))
            except Exception:
                logger.exception(
                    "Error in async receiver %s for signal '%s'",
                    cb.__qualname__, self.name,
                )
        return responses

    # Alias used by SQLAlchemy bridge (always a sync context).
    send_from_orm = send

    def __repr__(self) -> str:
        return f"<Signal name={self.name!r} receivers={len(self._receivers)}>"


# ---------------------------------------------------------------------------
# Async-from-sync helper
# ---------------------------------------------------------------------------

def _schedule_async(coro_func: Callable, **kwargs) -> None:
    """
    Safely schedule an async receiver from a synchronous (ORM-event) context.

    * Running loop  → ``loop.create_task(...)``  (fire-and-forget on the loop)
    * No loop       → ``asyncio.run(...)``        (blocks until done)
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro_func(**kwargs))
    except RuntimeError:
        try:
            asyncio.run(coro_func(**kwargs))
        except Exception:
            logger.exception(
                "Error running async receiver %s (no event loop)",
                coro_func.__qualname__,
            )


# ---------------------------------------------------------------------------
# @receiver decorator
# ---------------------------------------------------------------------------

def receiver(signal: Signal, sender: Any = None) -> Callable:
    """
    Decorator to connect a function (sync or async) to a signal::

        @receiver(post_save, sender=Event)
        async def handle_event_saved(sender, instance, created, **kwargs):
            ...
    """
    def decorator(func: Callable) -> Callable:
        signal.connect(func, sender=sender)
        return func
    return decorator


# ---------------------------------------------------------------------------
# SQLAlchemy ORM → Signal bridge
# ---------------------------------------------------------------------------

def register_model_signals(model: type) -> None:
    """
    Attach SQLAlchemy ORM event listeners to *model* so the built-in signals
    fire automatically.  Calling this more than once for the same model is
    a no-op.

    Mapping::

        before_insert  → pre_save(created=True)
        after_insert   → post_save(created=True)
        before_update  → pre_save(created=False)
        after_update   → post_save(created=False)
        before_delete  → pre_delete
        after_delete   → post_delete
        init           → post_init
    """
    if model in _registered_models:
        return
    _registered_models.add(model)

    @sa_event.listens_for(model, "before_insert")
    def _before_insert(mapper, connection, target):
        pre_save.send(sender=type(target), instance=target, created=True)

    @sa_event.listens_for(model, "before_update")
    def _before_update(mapper, connection, target):
        pre_save.send(sender=type(target), instance=target, created=False)

    @sa_event.listens_for(model, "after_insert")
    def _after_insert(mapper, connection, target):
        post_save.send(sender=type(target), instance=target, created=True)

    @sa_event.listens_for(model, "after_update")
    def _after_update(mapper, connection, target):
        post_save.send(sender=type(target), instance=target, created=False)

    @sa_event.listens_for(model, "before_delete")
    def _before_delete(mapper, connection, target):
        pre_delete.send(sender=type(target), instance=target)

    @sa_event.listens_for(model, "after_delete")
    def _after_delete(mapper, connection, target):
        post_delete.send(sender=type(target), instance=target)

    @sa_event.listens_for(model, "init")
    def _init(target, args, kwargs_):
        post_init.send(sender=type(target), instance=target)

    logger.debug("Registered SQLAlchemy signals for model '%s'", model.__name__)


def register_all_model_signals(Base) -> None:
    """
    Register SQLAlchemy signal listeners for every mapped model in *Base*.

    Safe to call multiple times — already-registered models are skipped::

        from fastapi_mvt.signals import register_all_model_signals
        from myproject.db import Base

        register_all_model_signals(Base)
    """
    for mapper in Base.registry.mappers:
        register_model_signals(mapper.class_)


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------

def autodiscover_signals(installed_apps: Sequence[str]) -> None:
    """
    Import ``{app}.signals`` for every app in *installed_apps*.

    Apps that do not have a ``signals`` module are silently skipped.
    This causes all ``@receiver`` decorators in those modules to execute,
    registering handlers with the built-in signals.

    Call this **before** ``register_all_model_signals`` so that receivers
    exist before the ORM listeners are attached::

        from fastapi_mvt.signals import autodiscover_signals
        from myproject.settings import Settings

        autodiscover_signals(Settings().INSTALLED_APPS)
    """
    for app in installed_apps:
        module_path = f"{app}.signals"
        try:
            importlib.import_module(module_path)
            logger.debug("Loaded signals from '%s'", module_path)
        except ModuleNotFoundError:
            pass  # app simply has no signals.py — that's fine
        except Exception:
            logger.exception("Error loading signals from '%s'", module_path)


def setup_signals(installed_apps: Sequence[str], Base) -> None:
    """
    One-call convenience that:

    1. Auto-discovers and imports ``{app}.signals`` for every app.
    2. Attaches SQLAlchemy ORM listeners to every model in *Base*.

    Typical usage in ``main.py`` startup::

        from fastapi_mvt.core.signals import setup_signals
        from myproject.db import Base
        from myproject.settings import Settings

        @app.on_event("startup")
        async def startup():
            setup_signals(Settings().INSTALLED_APPS, Base)
    """
    autodiscover_signals(installed_apps)
    register_all_model_signals(Base)


# ---------------------------------------------------------------------------
# Built-in signals
# ---------------------------------------------------------------------------

pre_save    = Signal("pre_save")
post_save   = Signal("post_save")
pre_delete  = Signal("pre_delete")
post_delete = Signal("post_delete")
post_init   = Signal("post_init")
