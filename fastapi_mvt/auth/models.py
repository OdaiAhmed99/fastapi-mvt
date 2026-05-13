"""
SQLAlchemy model mixin and default concrete model for fastapi-mvt auth.

AbstractAuthUser
----------------
A SQLAlchemy mixin that provides the standard auth columns.  Because
``__abstract__ = True``, SQLAlchemy will NOT create a table for this class
itself — only for concrete subclasses.

How to extend in your project (zero extra tables)::

    # accounts/models.py
    from sqlalchemy import Column, String
    from fastapi_mvt.auth.models import AbstractAuthUser
    from fastapi_mvt.db import Base

    class User(Base, AbstractAuthUser):
        __tablename__ = "users"          # ← only this table is created
        full_name = Column(String(120), nullable=True)
        phone     = Column(String(20),  nullable=True)

Then tell the library about your model::

    configure_auth(AuthConfig(
        secret_key="...",
        user_model="accounts.models.User",
    ))

DefaultAuthUser
---------------
Used automatically when no custom ``user_model`` is configured.
Table name: ``auth_users``.
"""

from __future__ import annotations

import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from fastapi_mvt.db import Base


class AbstractAuthUser:
    """
    Mixin that adds all standard auth fields to a SQLAlchemy model.

    Subclass together with ``Base`` in your project to get a fully featured
    user table without duplicating column definitions.
    """

    __abstract__ = True  # SQLAlchemy: no table is created for this mixin

    id              = Column(Integer, primary_key=True, index=True)
    email           = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    is_active       = Column(Boolean, default=True,  nullable=False)
    is_verified     = Column(Boolean, default=False, nullable=False)
    is_superuser    = Column(Boolean, default=False, nullable=False)
    created_at      = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        nullable=False,
    )
    updated_at      = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.__class__.__name__} id={self.id} email={self.email!r}>"


class DefaultAuthUser(Base, AbstractAuthUser):
    """
    Concrete user model used when no custom user model is configured.

    Table name: ``auth_users``.

    If you need project-specific fields or a different table name, define your
    own model using :class:`AbstractAuthUser` and configure the library via
    ``AuthConfig(user_model="yourapp.models.User")``.
    """

    __tablename__ = "auth_users"


__all__ = ["AbstractAuthUser", "DefaultAuthUser"]
