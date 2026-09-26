"""Django-style field helpers.

Each helper returns an ordinary SQLAlchemy ``mapped_column`` with the right
type and options, typed as ``Mapped[...]`` so editors and type checkers know
the attribute's type without an annotation::

    class Product(TimestampedModel):
        name = CharField(max_length=200)
        slug = SlugField(unique=True)
        description = TextField(null=True)
        price = DecimalField(max_digits=10, decimal_places=2)
        stock = IntegerField(default=0)
        active = BooleanField(default=True)
        released = DateField(null=True)
        category_id = ForeignKey(Category, on_delete=PROTECT)

This is exactly equivalent to the SQLAlchemy spelling
``name: Mapped[str] = mapped_column(String(200))``; use whichever you prefer,
even both in one model. As in Django, fields are NOT NULL unless ``null=True``.
"""

from __future__ import annotations

import datetime as dt
import decimal
import enum
import uuid as uuid_module
from typing import Any, Literal, Optional, TypeVar, overload

from sqlalchemy import JSON, BigInteger, Boolean, Date, Float, Integer, Numeric, String, Text, Time, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from fastapi_mvt.db.models import UTCDateTime, fk, utcnow

E = TypeVar("E", bound=enum.Enum)


def _column(
    type_: Any,
    *,
    null: bool,
    default: Any,
    unique: bool,
    index: bool,
    db_default: Any = None,
    help_text: Optional[str] = None,
    **extra: Any,
) -> Any:
    options: dict[str, Any] = dict(nullable=null, unique=unique or None, index=index or None, **extra)
    if default is not _MISSING:
        options["default"] = default
    if db_default is not None:
        options["server_default"] = db_default if isinstance(db_default, str) else str(db_default)
    if help_text:
        options["doc"] = help_text
    return mapped_column(type_, **{k: v for k, v in options.items() if v is not None or k == "nullable"})


class _Missing:
    def __repr__(self) -> str:
        return "<no default>"


_MISSING: Any = _Missing()


# The overloads make `null=True` fields `Mapped[Optional[T]]` and the rest `Mapped[T]`.


@overload
def CharField(max_length: int, *, null: Literal[False] = ..., default: Any = ..., unique: bool = ..., index: bool = ..., db_default: Any = ..., help_text: Optional[str] = ...) -> Mapped[str]: ...
@overload
def CharField(max_length: int, *, null: Literal[True], default: Any = ..., unique: bool = ..., index: bool = ..., db_default: Any = ..., help_text: Optional[str] = ...) -> Mapped[Optional[str]]: ...
def CharField(max_length: int, *, null: bool = False, default: Any = _MISSING, unique: bool = False, index: bool = False, db_default: Any = None, help_text: Optional[str] = None) -> Any:  # noqa: N802
    """Text with a maximum length (VARCHAR). ``max_length`` also limits API input."""
    return _column(String(max_length), null=null, default=default, unique=unique, index=index, db_default=db_default, help_text=help_text)


@overload
def EmailField(max_length: int = ..., *, null: Literal[False] = ..., default: Any = ..., unique: bool = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[str]: ...
@overload
def EmailField(max_length: int = ..., *, null: Literal[True], default: Any = ..., unique: bool = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[Optional[str]]: ...
def EmailField(max_length: int = 254, *, null: bool = False, default: Any = _MISSING, unique: bool = False, index: bool = False, help_text: Optional[str] = None) -> Any:  # noqa: N802
    return _column(String(max_length), null=null, default=default, unique=unique, index=index, help_text=help_text)


@overload
def SlugField(max_length: int = ..., *, null: Literal[False] = ..., default: Any = ..., unique: bool = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[str]: ...
@overload
def SlugField(max_length: int = ..., *, null: Literal[True], default: Any = ..., unique: bool = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[Optional[str]]: ...
def SlugField(max_length: int = 50, *, null: bool = False, default: Any = _MISSING, unique: bool = False, index: bool = True, help_text: Optional[str] = None) -> Any:  # noqa: N802
    return _column(String(max_length), null=null, default=default, unique=unique, index=index, help_text=help_text)


@overload
def TextField(*, null: Literal[False] = ..., default: Any = ..., help_text: Optional[str] = ...) -> Mapped[str]: ...
@overload
def TextField(*, null: Literal[True], default: Any = ..., help_text: Optional[str] = ...) -> Mapped[Optional[str]]: ...
def TextField(*, null: bool = False, default: Any = _MISSING, help_text: Optional[str] = None) -> Any:  # noqa: N802
    """Text of any length."""
    return _column(Text(), null=null, default=default, unique=False, index=False, help_text=help_text)


@overload
def IntegerField(*, null: Literal[False] = ..., default: Any = ..., unique: bool = ..., index: bool = ..., db_default: Any = ..., help_text: Optional[str] = ...) -> Mapped[int]: ...
@overload
def IntegerField(*, null: Literal[True], default: Any = ..., unique: bool = ..., index: bool = ..., db_default: Any = ..., help_text: Optional[str] = ...) -> Mapped[Optional[int]]: ...
def IntegerField(*, null: bool = False, default: Any = _MISSING, unique: bool = False, index: bool = False, db_default: Any = None, help_text: Optional[str] = None) -> Any:  # noqa: N802
    return _column(Integer(), null=null, default=default, unique=unique, index=index, db_default=db_default, help_text=help_text)


@overload
def BigIntegerField(*, null: Literal[False] = ..., default: Any = ..., unique: bool = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[int]: ...
@overload
def BigIntegerField(*, null: Literal[True], default: Any = ..., unique: bool = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[Optional[int]]: ...
def BigIntegerField(*, null: bool = False, default: Any = _MISSING, unique: bool = False, index: bool = False, help_text: Optional[str] = None) -> Any:  # noqa: N802
    return _column(BigInteger(), null=null, default=default, unique=unique, index=index, help_text=help_text)


@overload
def FloatField(*, null: Literal[False] = ..., default: Any = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[float]: ...
@overload
def FloatField(*, null: Literal[True], default: Any = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[Optional[float]]: ...
def FloatField(*, null: bool = False, default: Any = _MISSING, index: bool = False, help_text: Optional[str] = None) -> Any:  # noqa: N802
    return _column(Float(), null=null, default=default, unique=False, index=index, help_text=help_text)


@overload
def DecimalField(max_digits: int, decimal_places: int, *, null: Literal[False] = ..., default: Any = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[decimal.Decimal]: ...
@overload
def DecimalField(max_digits: int, decimal_places: int, *, null: Literal[True], default: Any = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[Optional[decimal.Decimal]]: ...
def DecimalField(max_digits: int, decimal_places: int, *, null: bool = False, default: Any = _MISSING, index: bool = False, help_text: Optional[str] = None) -> Any:  # noqa: N802
    """Exact decimal numbers, e.g. money: ``DecimalField(max_digits=10, decimal_places=2)``."""
    return _column(Numeric(max_digits, decimal_places), null=null, default=default, unique=False, index=index, help_text=help_text)


def BooleanField(*, default: bool = False, db_default: Optional[bool] = None, index: bool = False, help_text: Optional[str] = None) -> Mapped[bool]:  # noqa: N802
    server = None if db_default is None else ("1" if db_default else "0")
    return _column(Boolean(), null=False, default=default, unique=False, index=index, db_default=server, help_text=help_text)


@overload
def DateTimeField(*, null: Literal[False] = ..., auto_now_add: bool = ..., auto_now: bool = ..., default: Any = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[dt.datetime]: ...
@overload
def DateTimeField(*, null: Literal[True], auto_now_add: bool = ..., auto_now: bool = ..., default: Any = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[Optional[dt.datetime]]: ...
def DateTimeField(*, null: bool = False, auto_now_add: bool = False, auto_now: bool = False, default: Any = _MISSING, index: bool = False, help_text: Optional[str] = None) -> Any:  # noqa: N802
    """A timezone-aware timestamp, stored and returned in UTC.

    ``auto_now_add=True`` sets it on creation; ``auto_now=True`` on every save.
    """
    extra: dict[str, Any] = {}
    if auto_now_add or auto_now:
        default = utcnow
    if auto_now:
        extra["onupdate"] = utcnow
    return _column(UTCDateTime(), null=null, default=default, unique=False, index=index, help_text=help_text, **extra)


@overload
def DateField(*, null: Literal[False] = ..., default: Any = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[dt.date]: ...
@overload
def DateField(*, null: Literal[True], default: Any = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[Optional[dt.date]]: ...
def DateField(*, null: bool = False, default: Any = _MISSING, index: bool = False, help_text: Optional[str] = None) -> Any:  # noqa: N802
    return _column(Date(), null=null, default=default, unique=False, index=index, help_text=help_text)


@overload
def TimeField(*, null: Literal[False] = ..., default: Any = ..., help_text: Optional[str] = ...) -> Mapped[dt.time]: ...
@overload
def TimeField(*, null: Literal[True], default: Any = ..., help_text: Optional[str] = ...) -> Mapped[Optional[dt.time]]: ...
def TimeField(*, null: bool = False, default: Any = _MISSING, help_text: Optional[str] = None) -> Any:  # noqa: N802
    return _column(Time(), null=null, default=default, unique=False, index=False, help_text=help_text)


@overload
def UUIDField(*, null: Literal[False] = ..., default: Any = ..., unique: bool = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[uuid_module.UUID]: ...
@overload
def UUIDField(*, null: Literal[True], default: Any = ..., unique: bool = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[Optional[uuid_module.UUID]]: ...
def UUIDField(*, null: bool = False, default: Any = _MISSING, unique: bool = False, index: bool = False, help_text: Optional[str] = None) -> Any:  # noqa: N802
    """A UUID; ``UUIDField(default=uuid.uuid4, unique=True)`` for random ids."""
    return _column(Uuid(), null=null, default=default, unique=unique, index=index, help_text=help_text)


@overload
def JSONField(*, null: Literal[False] = ..., default: Any = ..., help_text: Optional[str] = ...) -> Mapped[Any]: ...
@overload
def JSONField(*, null: Literal[True], default: Any = ..., help_text: Optional[str] = ...) -> Mapped[Optional[Any]]: ...
def JSONField(*, null: bool = False, default: Any = _MISSING, help_text: Optional[str] = None) -> Any:  # noqa: N802
    """JSON data. Use ``default=dict`` / ``default=list`` (a callable), never a shared ``{}``."""
    if isinstance(default, (dict, list)):
        raise ValueError("Pass default=dict / default=list (a callable), not a shared mutable object.")
    return _column(JSON(), null=null, default=default, unique=False, index=False, help_text=help_text)


@overload
def EnumField(enum_class: type[E], *, null: Literal[False] = ..., default: Any = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[E]: ...
@overload
def EnumField(enum_class: type[E], *, null: Literal[True], default: Any = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[Optional[E]]: ...
def EnumField(enum_class: type[E], *, null: bool = False, default: Any = _MISSING, index: bool = False, help_text: Optional[str] = None) -> Any:  # noqa: N802
    """A Python ``Enum``, stored as its value in a VARCHAR (Django ``choices``).

    A default also becomes the database default, so the column can be added to a
    table that already has rows.
    """
    type_ = SAEnum(
        enum_class,
        native_enum=False,
        create_constraint=False,
        length=max(50, *(len(str(m.value)) for m in enum_class)),
        values_callable=lambda members: [m.value for m in members],
    )
    db_default = default.value if isinstance(default, enum.Enum) else None
    return _column(type_, null=null, default=default, unique=False, index=index, db_default=db_default, help_text=help_text)


@overload
def ForeignKey(to: Any, *, on_delete: str, null: Literal[False] = ..., index: bool = ..., help_text: Optional[str] = ...) -> Mapped[int]: ...
@overload
def ForeignKey(to: Any, *, on_delete: str, null: Literal[True], index: bool = ..., help_text: Optional[str] = ...) -> Mapped[Optional[int]]: ...
def ForeignKey(to: Any, *, on_delete: str, null: bool = False, index: bool = True, help_text: Optional[str] = None) -> Any:  # noqa: N802
    """The id column of a foreign key: ``author_id = ForeignKey(Author, on_delete=CASCADE)``.

    Unlike Django this declares only the ``_id`` column; add the Python-side
    link with ``author: Mapped[Author] = relationship()``. ``on_delete=SET_NULL``
    needs ``null=True``, as in Django.
    """
    if on_delete.upper().replace("_", " ") == "SET NULL" and not null:
        raise ValueError("on_delete=SET_NULL needs null=True (the column must be able to hold NULL).")
    extra = {"doc": help_text} if help_text else {}
    return fk(to, on_delete=on_delete, index=index, nullable=null, **extra)


__all__ = [
    "BigIntegerField",
    "BooleanField",
    "CharField",
    "DateField",
    "DateTimeField",
    "DecimalField",
    "EmailField",
    "EnumField",
    "FloatField",
    "ForeignKey",
    "IntegerField",
    "JSONField",
    "SlugField",
    "TextField",
    "TimeField",
    "UUIDField",
]

