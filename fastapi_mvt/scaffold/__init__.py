"""Project and app generators.

Templates are ordinary files under ``scaffold/project`` and ``scaffold/app``:

* ``{{name}}`` inserts a value
* ``{% flag %} ... {% else %} ... {% end %}`` keeps a block when *flag* is true
* ``__project__`` / ``__app__`` in a path is replaced by the name

Generators never overwrite an existing file.
"""

from __future__ import annotations

import keyword
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi_mvt.db.models import default_table_name

TEMPLATES = Path(__file__).parent
RENAMES = {"dotenv": ".env", "dotenv.example": ".env.example", "gitignore": ".gitignore"}
ROUTER_MARKER = "# Routers. `startapp` adds new apps below this line."

_TAG = re.compile(r"{%\s*(\w+)\s*%}")
_STANDALONE_TAG = re.compile(r"^[ \t]*({%\s*\w+\s*%})[ \t]*\r?\n", re.MULTILINE)
_VAR = re.compile(r"{{\s*(\w+)\s*}}")


class ScaffoldError(Exception):
    pass


def render(text: str, context: Mapping[str, object]) -> str:
    text = _STANDALONE_TAG.sub(r"\1", text)
    out: list[str] = []
    stack: list[tuple[bool, bool]] = []  # (condition, parent_emitting)
    emitting = True
    position = 0
    for match in _TAG.finditer(text):
        if emitting:
            out.append(text[position : match.start()])
        name = match.group(1)
        if name == "else":
            condition, parent = stack[-1]
            emitting = parent and not condition
        elif name == "end":
            _, emitting = stack.pop()
        else:
            if name not in context:
                raise KeyError(f"Unknown template flag {name!r}")
            condition = bool(context[name])
            stack.append((condition, emitting))
            emitting = emitting and condition
        position = match.end()
    if emitting:
        out.append(text[position:])
    if stack:
        raise ValueError("Unclosed {% %} block in template")
    return _VAR.sub(lambda m: str(context[m.group(1)]), "".join(out))


def _plan(template_dir: Path, target: Path, context: Mapping[str, object], names: Mapping[str, str]) -> list[tuple[Path, Path]]:
    files = []
    for source in sorted(template_dir.rglob("*.tmpl")):
        relative = source.relative_to(template_dir).as_posix()[: -len(".tmpl")]
        for placeholder, value in names.items():
            relative = relative.replace(placeholder, value)
        parts = relative.split("/")
        parts[-1] = RENAMES.get(parts[-1], parts[-1])
        files.append((source, target.joinpath(*parts)))
    return files


def _write(files: list[tuple[Path, Path]], context: Mapping[str, object]) -> list[Path]:
    existing = [dest for _, dest in files if dest.exists()]
    if existing:
        raise ScaffoldError(
            "Refusing to overwrite existing files:\n" + "\n".join(f"  {p}" for p in existing)
        )
    written = []
    for source, dest in files:
        dest.parent.mkdir(parents=True, exist_ok=True)
        content = render(source.read_text(encoding="utf-8"), context)
        dest.write_text(content, encoding="utf-8", newline="\n")
        written.append(dest)
    return written


def validate_identifier(name: str, kind: str) -> None:
    if not name.isidentifier() or keyword.iskeyword(name) or not name.islower():
        raise ScaffoldError(f"{kind} name {name!r} must be a lowercase Python identifier, e.g. 'blog' or 'shop_api'.")
    import importlib.util

    if importlib.util.find_spec(name) is not None and name not in ("users",):
        raise ScaffoldError(f"{name!r} conflicts with an existing Python module. Choose another name.")


def start_project(name: str, directory: Path) -> Path:
    from fastapi_mvt import __version__

    validate_identifier(name, "Project")
    target = directory / name
    if target.exists() and any(target.iterdir()):
        raise ScaffoldError(f"{target} already exists and is not empty.")
    context = {"project": name, "secret_key": secrets.token_urlsafe(50), "version": __version__}
    _write(_plan(TEMPLATES / "project", target, context, {"__project__": name}), context)
    return target


@dataclass
class AppResult:
    files: list[Path]
    router_registered: bool
    router_lines: list[str]


def start_app(name: str, project_root: Path, project_package: str, *, crud_model: Optional[str] = None) -> AppResult:
    validate_identifier(name, "App")
    if crud_model is not None and not re.fullmatch(r"[A-Z][A-Za-z0-9]*", crud_model):
        raise ScaffoldError(f"Model name {crud_model!r} must be CapitalizedCamelCase, e.g. 'Post'.")
    table = default_table_name(crud_model) if crud_model else name
    has_auth = (project_root / project_package / "auth.py").exists()
    context = {
        "app": name,
        "project": project_package,
        "crud": crud_model is not None,
        "auth": has_auth,
        "Model": crud_model or "",
        "singular": re.sub(r"(?<!^)(?=[A-Z])", "_", crud_model).lower() if crud_model else name,
        "plural": table,
        "url": table.replace("_", "-"),
    }
    files = _write(_plan(TEMPLATES / "app", project_root, context, {"__app__": name}), context)

    lines = [f"from {name}.router import router as {name}_router", f"app.include_router({name}_router)"]
    registered = _register_router(project_root / project_package / "main.py", lines)
    return AppResult(files, registered, lines)


def _register_router(main_py: Path, lines: list[str]) -> bool:
    """Add the import next to the other imports and include the router below the marker."""
    import ast

    if not main_py.exists():
        return False
    source = main_py.read_text(encoding="utf-8")
    import_line, include_line = lines
    if ROUTER_MARKER not in source or include_line in source:
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    last_import = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import = node.end_lineno or node.lineno
        elif not (isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant)):
            break  # imports end at the first real statement
    source_lines = source.splitlines()
    source_lines.insert(last_import, import_line)
    text = "\n".join(source_lines).rstrip("\n") + "\n" + include_line + "\n"
    main_py.write_text(text, encoding="utf-8", newline="\n")
    return True
