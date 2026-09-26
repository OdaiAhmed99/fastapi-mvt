"""The fastapi-mvt data layer: Django-style models and queries on SQLAlchemy 2.x.

::

    from fastapi_mvt.db import TimestampedModel, Mapped, mapped_column, fk, relationship, CASCADE

    class Author(TimestampedModel):
        name: Mapped[str] = mapped_column(String(100))
        posts: Mapped[list["Post"]] = relationship(back_populates="author")

    class Post(TimestampedModel):
        title: Mapped[str] = mapped_column(String(200))
        author_id: Mapped[int] = fk(Author, on_delete=CASCADE)
        author: Mapped[Author] = relationship(back_populates="posts")

    await Post.objects.filter(author__name="Ada").prefetch("author").order_by("-created_at")
"""

from sqlalchemy.orm import Mapped, mapped_column

from fastapi_mvt.db.database import (
    Database,
    DatabaseSessionMiddleware,
    async_to_sync,
    atomic,
    commit_on_error,
    current_session,
    get_database,
    in_transaction,
    on_commit,
    to_async_url,
)
from fastapi_mvt.db.exceptions import (
    FieldError,
    MultipleObjectsReturned,
    ObjectDoesNotExist,
    ORMError,
    RelationNotLoaded,
    SessionError,
)
from fastapi_mvt.db.fields import (
    BigIntegerField,
    BooleanField,
    CharField,
    DateField,
    DateTimeField,
    DecimalField,
    EmailField,
    EnumField,
    FloatField,
    ForeignKey,
    IntegerField,
    JSONField,
    SlugField,
    TextField,
    TimeField,
    UUIDField,
)
from fastapi_mvt.db.models import (
    CASCADE,
    NAMING_CONVENTION,
    NO_ACTION,
    PROTECT,
    RESTRICT,
    SET_DEFAULT,
    SET_NULL,
    Base,
    Manager,
    Model,
    TimestampedModel,
    UTCDateTime,
    default_table_name,
    fk,
    private,
    relationship,
    utcnow,
)
from fastapi_mvt.db.pagination import Page, PageParams
from fastapi_mvt.db.query import Q, QuerySet
from fastapi_mvt.db.schemas import ModelSchema, Schema, model_schema, schema_fields

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
    "Base",
    "CASCADE",
    "Database",
    "DatabaseSessionMiddleware",
    "FieldError",
    "Manager",
    "ModelSchema",
    "Mapped",
    "Model",
    "MultipleObjectsReturned",
    "NAMING_CONVENTION",
    "NO_ACTION",
    "ORMError",
    "ObjectDoesNotExist",
    "PROTECT",
    "Page",
    "PageParams",
    "Q",
    "QuerySet",
    "RESTRICT",
    "RelationNotLoaded",
    "SET_DEFAULT",
    "SET_NULL",
    "Schema",
    "SessionError",
    "TimestampedModel",
    "UTCDateTime",
    "async_to_sync",
    "atomic",
    "commit_on_error",
    "current_session",
    "default_table_name",
    "fk",
    "get_database",
    "in_transaction",
    "mapped_column",
    "model_schema",
    "schema_fields",
    "on_commit",
    "private",
    "relationship",
    "to_async_url",
    "utcnow",
]
