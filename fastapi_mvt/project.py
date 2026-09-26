"""Locate the project and load its pieces.

A project is the directory whose ``pyproject.toml`` has a
``[tool.fastapi-mvt]`` table::

    [tool.fastapi-mvt]
    app = "myproject.main:app"        # the FastAPI app (runserver, tests)
    database = "myproject.db:db"      # the Database object (migrations, tests)
    models = "auto"                   # or an explicit list of modules

With ``models = "auto"`` every top-level package that has a ``models.py``
(or ``models/`` package) is imported. Import errors are never swallowed: a
missing model during ``makemigrations`` would otherwise look like a deleted
table and generate a DROP TABLE.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

SKIP_DIRS = {"migrations", "tests", "test", "node_modules", "build", "dist", "docs", "site-packages"}


class ProjectError(RuntimeError):
    """The project configuration is missing or invalid."""


class ModelImportError(ImportError):
    """A models module could not be imported."""


@dataclass
class ProjectConfig:
    root: Path
    app: str
    database: str
    models: Any = "auto"
    migrations_dir: Path = field(default=Path("migrations"))

    @property
    def migrations_path(self) -> Path:
        return self.root / self.migrations_dir

    @property
    def package(self) -> str:
        return self.database.split(":")[0].split(".")[0]


def find_project(start: Optional[Path] = None) -> Optional[ProjectConfig]:
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        pyproject = directory / "pyproject.toml"
        if not pyproject.is_file():
            continue
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
        table = data.get("tool", {}).get("fastapi-mvt")
        if table is None:
            continue
        missing = [k for k in ("app", "database") if k not in table]
        if missing:
            raise ProjectError(f"[tool.fastapi-mvt] in {pyproject} is missing: {', '.join(missing)}")
        return ProjectConfig(
            root=directory,
            app=table["app"],
            database=table["database"],
            models=table.get("models", "auto"),
            migrations_dir=Path(table.get("migrations", "migrations")),
        )
    return None


def get_project(start: Optional[Path] = None) -> ProjectConfig:
    project = find_project(start)
    if project is None:
        raise ProjectError(
            "No fastapi-mvt project found. Run this command inside a project "
            "(a directory whose pyproject.toml has a [tool.fastapi-mvt] section), "
            "or create one with `fastapi-mvt startproject <name>`."
        )
    activate(project)
    return project


def activate(project: ProjectConfig) -> None:
    root = str(project.root)
    if root not in sys.path:
        sys.path.insert(0, root)


def import_string(path: str) -> Any:
    """Import ``"package.module:attribute"``."""
    if ":" not in path:
        raise ProjectError(f"Expected 'module:attribute', got {path!r}")
    module_name, attribute = path.split(":", 1)
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attribute)
    except AttributeError:
        raise ProjectError(f"{module_name!r} has no attribute {attribute!r}") from None


def discover_modules(root: Path, name: str) -> list[str]:
    """``<package>.<name>`` for every top-level package that has that module."""
    modules = []
    for child in sorted(root.iterdir()):
        if (
            not child.is_dir()
            or child.name.startswith((".", "_"))
            or child.name in SKIP_DIRS
            or (child / "pyvenv.cfg").exists()
            or not (child / "__init__.py").exists()
        ):
            continue
        if (child / f"{name}.py").exists() or (child / name / "__init__.py").exists():
            modules.append(f"{child.name}.{name}")
    return modules


def discover_model_modules(root: Path) -> list[str]:
    return discover_modules(root, "models")


def model_modules(project: ProjectConfig) -> list[str]:
    if project.models == "auto":
        return discover_model_modules(project.root)
    if isinstance(project.models, str):
        return [project.models]
    return list(project.models)


_loaded: set[str] = set()


def load_project_models(project: Optional[ProjectConfig] = None, *, required: bool = True) -> list[str]:
    """Import every models module of the project; raise loudly if one is broken."""
    if project is None:
        project = get_project() if required else find_project()
        if project is None:
            return []
    activate(project)
    modules = model_modules(project)
    for name in modules:
        if name in _loaded:
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:
            raise ModelImportError(
                f"Could not import models module {name!r}: {type(exc).__name__}: {exc}\n"
                "Fix this error first. fastapi-mvt refuses to continue, because a model "
                "that failed to import would look like a deleted table to migrations."
            ) from exc
        _loaded.add(name)
    return modules


def load_database(project: Optional[ProjectConfig] = None) -> Any:
    from fastapi_mvt.db import Database

    project = project or get_project()
    database = import_string(project.database)
    if not isinstance(database, Database):
        raise ProjectError(f"{project.database} is a {type(database).__name__}, expected fastapi_mvt.db.Database")
    return database


def load_app(project: Optional[ProjectConfig] = None) -> Any:
    project = project or get_project()
    return import_string(project.app)
