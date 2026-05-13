"""
SQLAlchemy 2.0 database utilities and ORM integration.
Supports both sync and async sessions.
"""

import os
from typing import AsyncGenerator, Generator
from sqlalchemy import create_engine, MetaData
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

# Standard naming convention required by Alembic batch mode (SQLite) and
# recommended for all databases so autogenerate never emits None constraint names.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Declarative base for all models
Base = declarative_base(metadata=MetaData(naming_convention=NAMING_CONVENTION))


def get_engine(database_url: str = None, echo: bool = False):
    """
    Create and return SQLAlchemy engine
    
    Args:
        database_url: Database connection URL
        echo: Whether to echo SQL statements
    
    Returns:
        SQLAlchemy Engine instance
    """
    if database_url is None:
        database_url = os.getenv("DATABASE_URL", "sqlite:///./app.db")
    
    # Special handling for SQLite
    if database_url.startswith("sqlite"):
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            echo=echo
        )
    else:
        engine = create_engine(database_url, echo=echo)
    
    return engine


def get_session_local(engine):
    """
    Create SessionLocal class for database sessions
    
    Args:
        engine: SQLAlchemy engine
    
    Returns:
        SessionLocal class
    """
    SessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine
    )
    return SessionLocal


def get_db(session_local) -> Generator[Session, None, None]:
    """
    FastAPI dependency for database sessions
    
    Usage:
        @router.get("/items")
        def get_items(db: Session = Depends(get_db)):
            return db.query(Item).all()
    
    Yields:
        Database session
    """
    db = session_local()
    try:
        yield db
    finally:
        db.close()


def create_tables(engine):
    """
    Create all tables in the database
    
    Args:
        engine: SQLAlchemy engine
    """
    Base.metadata.create_all(bind=engine)


# ── Async support ─────────────────────────────────────────────────────────────

def _to_async_url(database_url: str) -> str:
    """Convert a plain sync driver URL to its async equivalent."""
    if database_url.startswith("sqlite:///"):
        return database_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    if database_url.startswith("sqlite://"):
        return database_url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return database_url  # already has async driver prefix


def get_async_engine(database_url: str = None, echo: bool = False):
    """
    Create and return an async SQLAlchemy engine.

    The URL is automatically converted to use the correct async driver:
      - sqlite → sqlite+aiosqlite
      - postgresql/postgres → postgresql+asyncpg

    Args:
        database_url: Database connection URL (sync or async form).
        echo: Whether to echo SQL statements.

    Returns:
        AsyncEngine instance.
    """
    if database_url is None:
        database_url = os.getenv("DATABASE_URL", "sqlite:///./app.db")
    async_url = _to_async_url(database_url)
    return create_async_engine(async_url, echo=echo)


def get_async_session_local(async_engine):
    """
    Create an async session factory.

    Args:
        async_engine: AsyncEngine instance.

    Returns:
        async_sessionmaker bound to the given engine.
    """
    return async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def get_async_db(async_session_local) -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI async dependency for database sessions.

    Usage::

        from {name}.db import async_session_local

        async def get_db():
            async for session in get_async_db(async_session_local):
                yield session

        @router.get("/items")
        async def list_items(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(Item))
            return result.scalars().all()

    Yields:
        AsyncSession
    """
    async with async_session_local() as session:
        try:
            yield session
        finally:
            await session.close()
