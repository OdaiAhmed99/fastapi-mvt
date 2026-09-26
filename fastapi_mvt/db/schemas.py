"""Pydantic schemas for models.

Derive a schema from a model's columns with :class:`ModelSchema`, and add or
override fields like on any pydantic model::

    class PostCreate(ModelSchema, model=Post, mode="create"):   # fields a client may send
        title: str = Field(min_length=3, max_length=200)         # explicit fields win

    class PostUpdate(ModelSchema, model=Post, mode="update"):   # same, all optional
        pass

    class PostRead(ModelSchema, model=Post):                    # every public column...
        author: AuthorRead                                      # ...plus a nested relation

Constraints come from the columns: ``String(200)`` becomes ``max_length=200``,
nullable columns become optional, and ``private()`` columns never appear.
Hand-written :class:`Schema` subclasses work everywhere too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic._internal._model_construction import ModelMetaclass
from sqlalchemy import JSON
from sqlalchemy import inspect as sa_inspect

Mode = Literal["read", "create", "update"]


class Schema(BaseModel):
    """Base for API schemas; reads attributes straight off model instances."""

    model_config = ConfigDict(from_attributes=True)


def _python_type(column: Any) -> Any:
    if isinstance(column.type, JSON):
        return Any
    for column_type in (column.type, getattr(column.type, "impl", None)):
        if column_type is None:
            continue
        try:
            return column_type.python_type
        except (NotImplementedError, AttributeError):
            continue
    return Any


def _is_server_generated(column: Any) -> bool:
    """Values the client never sends: auto-increment keys and computed/identity columns."""
    if column.computed is not None or column.identity is not None:
        return True
    return bool(column.primary_key and column.autoincrement in (True, "auto") and _python_type(column) is int)


def schema_fields(
    model: type,
    mode: Mode = "read",
    *,
    include: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> dict[str, tuple[Any, Any]]:
    """The ``{name: (annotation, FieldInfo)}`` a schema for *model* in *mode* gets."""
    if mode not in ("read", "create", "update"):
        raise ValueError(f"mode must be 'read', 'create' or 'update', got {mode!r}")
    mapper: Any = sa_inspect(model)
    exclude_set = set(exclude or [])
    automatic = {"created_at", "updated_at"}
    fields: dict[str, Any] = {}

    for attr in mapper.column_attrs:
        if len(attr.columns) != 1:
            continue
        column = attr.columns[0]
        key = attr.key
        if key in exclude_set or (include is not None and key not in include):
            continue
        if column.info.get("private"):
            continue
        if mode != "read" and (_is_server_generated(column) or (key in automatic and column.default is not None)):
            continue

        annotation = _python_type(column)
        constraints: dict[str, Any] = {}
        length = getattr(column.type, "length", None)
        if isinstance(length, int) and annotation is str:
            constraints["max_length"] = length
        if column.doc:
            constraints["description"] = column.doc

        # In "update" mode every field may be left out, but only nullable columns
        # accept an explicit null (a null for a NOT NULL column is a 422, not a 409).
        optional = bool(column.nullable)
        if optional:
            annotation = Optional[annotation]

        if mode == "update":
            default: Any = None
        elif mode == "create" and column.default is not None and column.default.is_scalar:
            default = column.default.arg
        elif mode == "create" and (column.default is not None or column.server_default is not None):
            default = None  # the callable / database default applies on insert
            annotation = Optional[annotation]
        elif optional:
            default = None
        else:
            default = ...
        fields[key] = (annotation, Field(default, **constraints))
    return fields


class _ModelSchemaMeta(ModelMetaclass):
    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        model: Optional[type] = None,
        mode: Mode = "read",
        include: Optional[list[str]] = None,
        exclude: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> Any:
        if model is not None:
            # The model's fields live in a generated parent class; the class being
            # defined inherits them and overrides any it declares itself. Nothing
            # touches the class body's annotations, so this works the same on every
            # Python version (3.14 stores class annotations differently).
            fields = schema_fields(model, mode, include=include, exclude=exclude)
            generated = create_model(  # type: ignore[call-overload]
                f"{name}Fields", __base__=Schema, __module__=namespace.get("__module__", __name__), **fields
            )
            bases = (generated, *bases)
        return super().__new__(mcs, name, bases, namespace, **kwargs)


class ModelSchema(Schema, metaclass=_ModelSchemaMeta):
    """A schema whose fields come from a model: ``class PostRead(ModelSchema, model=Post)``.

    ``mode`` is ``"read"`` (default), ``"create"`` or ``"update"``; ``include`` /
    ``exclude`` pick columns. Declared fields override the generated ones.
    """

    if TYPE_CHECKING:
        # Fields are generated at runtime, so let type checkers accept them.
        def __init__(self, **data: Any) -> None: ...

        def __getattr__(self, name: str) -> Any: ...

        def __init_subclass__(
            cls,
            *,
            model: Optional[type] = None,
            mode: Mode = "read",
            include: Optional[list[str]] = None,
            exclude: Optional[list[str]] = None,
            **kwargs: Any,
        ) -> None: ...


def model_schema(
    model: type,
    mode: Mode = "read",
    *,
    include: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
    name: Optional[str] = None,
) -> type[Schema]:
    """Build a schema class in one call: ``PostRead = model_schema(Post)``.

    To extend it, prefer ``class PostRead(ModelSchema, model=Post)``, which type
    checkers understand.
    """
    fields = schema_fields(model, mode, include=include, exclude=exclude)
    suffix = {"read": "Read", "create": "Create", "update": "Update"}[mode]
    return create_model(name or f"{model.__name__}{suffix}", __base__=Schema, **fields)  # type: ignore[call-overload, no-any-return]
