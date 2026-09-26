"""Alembic environment, managed by fastapi-mvt.

Migration behaviour lives in fastapi_mvt.migrations.run_migrations, so it is
tested once and upgrades with the library. Configure it in pyproject.toml
under [tool.fastapi-mvt].
"""

from fastapi_mvt.migrations import run_migrations

run_migrations()
