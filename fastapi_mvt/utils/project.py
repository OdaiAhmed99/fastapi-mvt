"""
Project discovery helpers for fastapi_mvt apps.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional, Tuple, Union


def get_project_root(start_path: Optional[Union[str, Path]] = None) -> Path:
    """
    Find the project root directory.

    If ``start_path`` is provided, it should usually be ``__file__`` from an
    app module. If omitted, the current working directory is used.
    """
    if start_path is not None:
        path = Path(start_path).resolve()
        current = path.parent if path.is_file() else path

        for candidate in (current, *current.parents):
            if (candidate / ".env").exists() or (candidate / "manage.py").exists():
                return candidate

        # Legacy app layout: app/module.py -> apps/app -> apps -> project root.
        parents = list(current.parents)
        if len(parents) >= 2:
            return parents[1]

    cwd = Path.cwd().resolve()
    if (cwd / "manage.py").exists() or (cwd / ".env").exists():
        return cwd

    for parent in cwd.parents:
        if (parent / "manage.py").exists() or (parent / ".env").exists():
            return parent

    return cwd


def discover_project_module(project_root: Optional[Union[str, Path]] = None) -> str:
    """
    Discover the project module name by finding a package with app settings.
    """
    root = get_project_root() if project_root is None else Path(project_root).resolve()

    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    excluded = {".", "..", "apps", "__pycache__", ".git", ".venv", "venv", "env"}

    for item in os.listdir(root):
        item_path = root / item
        if (
            item_path.is_dir()
            and not item.startswith(".")
            and item not in excluded
            and (item_path / "__init__.py").exists()
            and ((item_path / "main.py").exists() or (item_path / "settings.py").exists())
        ):
            return item

    raise ImportError(
        "Could not find project module. "
        "Make sure you're running from the project root directory."
    )


def get_db_session(project_name: Optional[str] = None):
    """
    Import and return the project's ``get_db`` dependency.
    """
    if project_name is None:
        project_name = discover_project_module()

    try:
        db_module = __import__(f"{project_name}.db", fromlist=["get_db"])
        return db_module.get_db
    except (ImportError, AttributeError) as exc:
        raise ImportError(f"Could not import get_db from {project_name}.db: {exc}")


def get_base_model(project_name: Optional[str] = None):
    """
    Import and return the project's SQLAlchemy declarative Base.
    """
    if project_name is None:
        project_name = discover_project_module()

    try:
        db_module = __import__(f"{project_name}.db", fromlist=["Base"])
        return db_module.Base
    except (ImportError, AttributeError) as exc:
        raise ImportError(f"Could not import Base from {project_name}.db: {exc}")


def setup_project_imports(file_path: Union[str, Path]) -> Tuple[str, Any, Any]:
    """
    Resolve common project imports for an app module.

    Returns ``(project_name, get_db, Base)``.
    """
    project_root = get_project_root(file_path)
    root_str = str(project_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    project_name = discover_project_module(project_root)
    get_db = get_db_session(project_name)
    Base = get_base_model(project_name)

    return project_name, get_db, Base


__all__ = [
    "get_project_root",
    "discover_project_module",
    "get_db_session",
    "get_base_model",
    "setup_project_imports",
]
