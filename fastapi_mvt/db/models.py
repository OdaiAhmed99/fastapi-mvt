"""Declarative base classes.

Models are ordinary SQLAlchemy 2.x mapped classes: everything SQLAlchemy,
Alembic and SQLAdmin can do with a model still works. On top of that each
model gets:

* an automatic ``__tablename__`` (``BlogPost`` -> ``blog_posts``)
* constraint names that migrations can rely on
* ``Model.objects`` -> a Django-style async :class:`~fastapi_mvt.db.query.QuerySet`
* ``Model.DoesNotExist`` / ``Model.MultipleObjectsReturned``
* ``await obj.save()``, ``await obj.update(...)``, ``await obj.delete()``,
  ``await obj.refresh()``, ``await obj.load("relation")``
"""

from __future__ import annotations

import enum
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Optional, TypeVar, overload

from sqlalchemy import JSON, DateTime, ForeignKey, MetaData, TypeDecorator
from sqlalchemy import Enum as SAEnum
from sqlalchemy import delete as sa_delete
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column
from sqlalchemy.orm import relationship as sa_relationship

from fastapi_mvt.db.database import session_scope
from fastapi_mvt.db.exceptions import MultipleObjectsReturned, ObjectDoesNotExist

if TYPE_CHECKING:
    from fastapi_mvt.db.query import QuerySet

# Deterministic constraint names: without them Alembic cannot drop or alter
# constraints (and SQLite batch migrations fail).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

M = TypeVar("M", bound="Base")
Q = TypeVar("Q", bound="QuerySet[Any]")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):  # type: ignore[type-arg]
    """A timestamp stored in UTC and always read back timezone-aware.

    SQLite has no timezone support and would otherwise return naive values
    (and silently store an aware time's wall clock). Naive input is taken as UTC.
    Migrations render it as ``sa.DateTime(timezone=True)``.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    @property
    def python_type(self) -> type:
        return datetime

    def process_bind_param(self, value: Optional[datetime], dialect: Any) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: Optional[datetime], dialect: Any) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def default_table_name(class_name: str) -> str:
    """``Category`` -> ``categories``, ``BlogPost`` -> ``blog_posts``, ``Address`` -> ``addresses``."""
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", class_name).lower()
    if re.search(r"[^aeiou]y$", snake):
        return snake[:-1] + "ies"
    if re.search(r"(s|x|z|ch|sh)$", snake):
        return snake + "es"
    return snake + "s"


class Manager(Generic[Q]):
    """``Model.objects``: returns a fresh QuerySet on every access.

    Use a custom QuerySet class for reusable filters::

        class PostQuerySet(QuerySet["Post"]):
            def published(self):
                return self.filter(published=True)

        class Post(Model):
            objects: ClassVar[Manager[PostQuerySet]] = Manager(PostQuerySet)
            ...

        await Post.objects.published().order_by("-created_at")
    """

    def __init__(self, queryset_class: Optional[type[Q]] = None) -> None:
        self.queryset_class = queryset_class

    @overload
    def __get__(self, instance: None, owner: type[M]) -> "QuerySet[M]": ...
    @overload
    def __get__(self, instance: object, owner: type[M]) -> "QuerySet[M]": ...

    def __get__(self, instance: Any, owner: Any) -> Any:
        if instance is not None:
            raise AttributeError(
                f"Manager isn't accessible via {owner.__name__} instances; use {owner.__name__}.objects"
            )
        from fastapi_mvt.db.query import QuerySet

        return (self.queryset_class or QuerySet)(owner)


class Base(AsyncAttrs, DeclarativeBase):
    """Root of every model. Subclass :class:`Model` unless you need no ``id`` column."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        datetime: UTCDateTime(),
        dict: JSON,
        list: JSON,
        # Enums are stored as their values in a VARCHAR: readable, portable, and adding
        # a member needs no ALTER TYPE (native PostgreSQL enums are painful to migrate).
        enum.Enum: SAEnum(
            enum.Enum,
            native_enum=False,
            create_constraint=False,
            length=50,
            values_callable=lambda members: [m.value for m in members],
        ),
    }

    # Fetch database-generated values (server defaults) right after INSERT/UPDATE:
    # reading an expired attribute later would need hidden I/O, impossible in async code.
    __mapper_args__ = {"eager_defaults": True}

    objects: ClassVar[Manager[Any]] = Manager()
    DoesNotExist: ClassVar[type[ObjectDoesNotExist]] = ObjectDoesNotExist
    MultipleObjectsReturned: ClassVar[type[MultipleObjectsReturned]] = MultipleObjectsReturned

    @declared_attr.directive
    def __tablename__(cls) -> str:  # noqa: N805
        return default_table_name(cls.__name__)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        name = cls.__name__
        cls.DoesNotExist = type(
            "DoesNotExist",
            (cls.DoesNotExist,),
            {"model_name": name, "__qualname__": f"{name}.DoesNotExist", "__module__": cls.__module__},
        )
        cls.MultipleObjectsReturned = type(
            "MultipleObjectsReturned",
            (cls.MultipleObjectsReturned,),
            {"__qualname__": f"{name}.MultipleObjectsReturned", "__module__": cls.__module__},
        )
        super().__init_subclass__(**kwargs)

    # ── Instance API ────────────────────────────────────────────────────────

    @property
    def pk(self) -> Any:
        identity = sa_inspect(self).identity
        if identity is None:
            return None
        return identity[0] if len(identity) == 1 else identity

    async def save(self: M, *, session: Optional[AsyncSession] = None) -> M:
        """INSERT or UPDATE this object. Database defaults and the id are available afterwards."""
        async with session_scope(session) as s:
            _attach(s, self)
            await s.flush()
        return self

    async def update(self: M, *, session: Optional[AsyncSession] = None, **fields: Any) -> M:
        """Set several fields and save: ``await post.update(title="New", published=True)``."""
        mapper = sa_inspect(type(self))
        for key, value in fields.items():
            if key not in mapper.attrs:
                raise AttributeError(f"{type(self).__name__} has no field {key!r}")
            setattr(self, key, value)
        return await self.save(session=session)

    async def delete(self, *, session: Optional[AsyncSession] = None) -> None:
        """DELETE this row. Related rows follow the foreign keys' ``on_delete`` rule."""
        state = sa_inspect(self)
        async with session_scope(session) as s:
            if state.pending:
                s.expunge(self)
                return
            if state.identity is None:
                raise ValueError(f"Cannot delete an unsaved {type(self).__name__}.")
            mapper = sa_inspect(type(self))
            conditions = [col == value for col, value in zip(mapper.primary_key, state.identity, strict=True)]
            await s.execute(
                sa_delete(type(self)).where(*conditions).execution_options(synchronize_session=False)
            )
            if state.session is s:
                s.expunge(self)

    async def refresh(self: M, *, session: Optional[AsyncSession] = None) -> M:
        """Reload all column values from the database."""
        async with session_scope(session) as s:
            _attach(s, self)
            await s.refresh(self)
        return self

    async def load(self: M, *relations: str, session: Optional[AsyncSession] = None) -> M:
        """Load relationships on an object you already have::

        await post.load("author", "tags")
        post.author.name
        """
        async with session_scope(session) as s:
            _attach(s, self)
            await s.refresh(self, attribute_names=list(relations))
        return self

    def to_dict(self, *, exclude: tuple[str, ...] = ()) -> dict[str, Any]:
        """Column values as a plain dict (relationships are not included)."""
        mapper = sa_inspect(type(self))
        return {
            attr.key: getattr(self, attr.key)
            for attr in mapper.column_attrs
            if attr.key not in exclude
        }

    def __repr__(self) -> str:
        return f"<{type(self).__name__} pk={self.pk!r}>"


def _attach(session: AsyncSession, obj: Base) -> None:
    owner = sa_inspect(obj).session
    if owner is None:
        session.add(obj)
    elif owner is not session.sync_session:
        raise ValueError(
            f"{obj!r} belongs to another open session. Pass session=... explicitly, "
            "or load the object inside the same transaction."
        )


class Model(Base):
    """A model with an integer ``id`` primary key (like Django's default)."""

    __abstract__ = True

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)


class TimestampedModel(Model):
    """A :class:`Model` plus ``created_at`` / ``updated_at``, maintained automatically."""

    __abstract__ = True

    created_at: Mapped[datetime] = mapped_column(default=utcnow, sort_order=100)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, sort_order=101)


# ── Field helpers ───────────────────────────────────────────────────────────

CASCADE = "CASCADE"
SET_NULL = "SET NULL"
RESTRICT = "RESTRICT"
PROTECT = "RESTRICT"
NO_ACTION = "NO ACTION"
SET_DEFAULT = "SET DEFAULT"
_ON_DELETE = {CASCADE, SET_NULL, RESTRICT, NO_ACTION, SET_DEFAULT}


def fk(target: Any, *, on_delete: str, index: bool = True, **kwargs: Any) -> Any:
    """A foreign-key column. ``on_delete`` is required, just like in Django::

        author_id: Mapped[int] = fk(Author, on_delete=CASCADE)
        editor_id: Mapped[int | None] = fk("User", on_delete=SET_NULL)

    *target* is a model class, a model name, or ``"table.column"``.
    The database enforces the rule, so it also applies to bulk deletes.
    """
    rule = on_delete.upper().replace("_", " ")
    if rule == "PROTECT":
        rule = RESTRICT
    if rule not in _ON_DELETE:
        raise ValueError(f"on_delete must be one of {sorted(_ON_DELETE)}, got {on_delete!r}")

    if isinstance(target, str):
        column_ref = target if "." in target else f"{default_table_name(target)}.id"
    else:
        mapper = sa_inspect(target)
        pk_columns = mapper.primary_key
        if len(pk_columns) != 1:
            raise ValueError(f"fk() needs a single-column primary key on {target.__name__}")
        column_ref = pk_columns[0]
    return mapped_column(ForeignKey(column_ref, ondelete=rule), index=index, **kwargs)


def _register_explicit_loader() -> str:
    """Register lazy="explicit": raise_on_sql, with an error message that says how to fix it.

    Uses SQLAlchemy's loader-strategy registry; if its internals ever change,
    fall back to the stock "raise_on_sql" (same behaviour, terser message).
    """
    try:
        from sqlalchemy.orm import strategies
        from sqlalchemy.orm.relationships import RelationshipProperty

        from fastapi_mvt.db.exceptions import DetachedRelationNotLoaded, RelationNotLoaded

        def _hint(loader: Any) -> str:
            owner = loader.parent.class_.__name__
            key = loader.key
            return (
                f"{owner}.{key} is not loaded. Load it with the query: "
                f'.prefetch("{key}") (or .select_related("{key}")), '
                f'or on this object: await obj.load("{key}").'
            )

        # Named LazyLoader in SQLAlchemy 2.0, _LazyLoader in 2.1.
        lazy_loader = getattr(strategies, "LazyLoader", None) or getattr(strategies, "_LazyLoader")  # noqa: B009

        @RelationshipProperty.strategy_for(lazy="explicit")
        class ExplicitLoader(lazy_loader):  # type: ignore[misc, valid-type]
            def __init__(self, parent: Any, strategy_key: Any) -> None:
                super().__init__(parent, strategy_key)
                self._raise_on_sql = True

            def _invoke_raise_load(self, state: Any, passive: Any, lazy: Any) -> None:
                raise RelationNotLoaded(_hint(self))

            def _load_for_state(self, state: Any, *args: Any, **kwargs: Any) -> Any:
                try:
                    return super()._load_for_state(state, *args, **kwargs)
                except RelationNotLoaded:
                    raise
                except Exception as exc:
                    if type(exc).__name__ == "DetachedInstanceError":
                        raise DetachedRelationNotLoaded(
                            _hint(self) + " (The object was loaded in a transaction that has ended; "
                            "load relations inside the same request or atomic() block.)"
                        ) from None
                    raise

        return "explicit"
    except Exception:  # pragma: no cover - only if SQLAlchemy's internals change
        return "raise_on_sql"


_EXPLICIT = _register_explicit_loader()


def relationship(*args: Any, **kwargs: Any) -> Any:
    """``sqlalchemy.orm.relationship`` with async-safe loading.

    Relationships are never loaded implicitly (implicit I/O is impossible in
    async code). Load them explicitly: ``.prefetch("author")`` on a query, or
    ``await obj.load("author")`` on an object. Reading one that wasn't loaded
    raises :class:`RelationNotLoaded`, which says exactly that.
    """
    kwargs.setdefault("lazy", _EXPLICIT)
    return sa_relationship(*args, **kwargs)


def private(*args: Any, **kwargs: Any) -> Any:
    """``mapped_column`` that ``model_schema`` and the admin never expose (e.g. password hashes)."""
    info = dict(kwargs.pop("info", {}) or {})
    info["private"] = True
    return mapped_column(*args, info=info, **kwargs)
