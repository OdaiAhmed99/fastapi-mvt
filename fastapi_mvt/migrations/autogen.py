"""Post-processing of Alembic autogenerate output (``makemigrations``).

Plain autogenerate is a diff: it cannot tell a renamed column from a dropped
one, and it happily writes migrations that will fail on real data. This
module adds the checks Django users rely on:

* **renames**: a dropped + added column (or table) pair triggers
  "Was posts.body renamed to posts.content?" and turns into a real rename
* **destructive changes**: dropping a table or column needs confirmation
  (or ``--allow-destructive`` when not interactive)
* **NOT NULL without default** on a table that has rows: asks for a one-off
  default, instead of producing a migration that crashes on ``migrate``
* **Django-like names**: ``0001_initial``, ``0002_add_views_to_posts``
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import sqlalchemy as sa
from alembic.autogenerate import renderers
from alembic.operations import ops

MAX_REVISION_LENGTH = 32  # alembic_version.version_num is VARCHAR(32)


@renderers.dispatch_for(ops.RenameTableOp)
def _render_rename_table(autogen_context: Any, op: ops.RenameTableOp) -> str:
    # Autogenerate never produces renames itself, so Alembic ships no renderer for them.
    schema = f", schema={op.schema!r}" if op.schema else ""
    return f"op.rename_table({op.table_name!r}, {op.new_table_name!r}{schema})"


_render_alter_column = renderers.dispatch(ops.AlterColumnOp("table", "column"))


def _render_alter_column_with_rename(autogen_context: Any, op: ops.AlterColumnOp) -> str:
    """Alembic's renderer, plus ``new_column_name`` for renames.

    Some Alembic versions (e.g. 1.14) leave ``new_column_name`` out, which turns
    a rename into a silent no-op. Add it, and refuse to write a broken rename.
    """
    rendered = _render_alter_column(autogen_context, op)
    if op.modify_name and "new_column_name" not in rendered:
        # batch_op.alter_column('body', ...)  or  op.alter_column('posts', 'body', ...)
        start = rendered.find("alter_column(")
        marker = f"{op.column_name!r},"
        position = rendered.find(marker, start)
        if start < 0 or position < 0:
            raise MigrationAborted(f"Could not render the rename of {op.table_name}.{op.column_name}.")
        position += len(marker)
        rendered = rendered[:position] + f"\n               new_column_name={op.modify_name!r}," + rendered[position:]
    return rendered


try:  # Alembic >= 1.15 can replace a renderer
    renderers.dispatch_for(ops.AlterColumnOp, replace=True)(_render_alter_column_with_rename)  # type: ignore[call-arg]
except TypeError:  # older Alembic: its registry is a plain dict
    renderers._registry[(ops.AlterColumnOp, "default")] = _render_alter_column_with_rename


class MigrationAborted(Exception):
    """makemigrations stopped without writing a file."""


@dataclass
class Prompter:
    interactive: bool
    confirm: Callable[[str], bool] = lambda text: False
    ask: Callable[[str], str] = lambda text: ""


@dataclass
class RevisionPlan:
    number: int
    is_initial: bool
    name: Optional[str] = None
    empty: bool = False
    allow_destructive: bool = False
    # False when comparing against a scratch database: assume tables may hold rows.
    row_counts_known: bool = True
    # Filled in by the hook:
    no_changes: bool = False
    revision: Optional[str] = None
    operations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ── Describing operations ───────────────────────────────────────────────────


def _walk(container: Any) -> list[Any]:
    found = []
    for op in getattr(container, "ops", []):
        if isinstance(op, ops.ModifyTableOps):
            found.extend(_walk(op))
        else:
            found.append(op)
    return found


def describe(op: Any) -> str:
    if isinstance(op, ops.CreateTableOp):
        return f"Create table {op.table_name}"
    if isinstance(op, ops.DropTableOp):
        return f"Drop table {op.table_name}"
    if isinstance(op, ops.RenameTableOp):
        return f"Rename table {op.table_name} to {op.new_table_name}"
    if isinstance(op, ops.AddColumnOp):
        return f"Add column {op.column.name} to {op.table_name}"
    if isinstance(op, ops.DropColumnOp):
        return f"Remove column {op.column_name} from {op.table_name}"
    if isinstance(op, ops.AlterColumnOp):
        if op.modify_name:
            return f"Rename column {op.table_name}.{op.column_name} to {op.modify_name}"
        changes = []
        if op.modify_type is not None:
            changes.append(f"type -> {op.modify_type}")
        if op.modify_nullable is not None:
            changes.append("NULL allowed" if op.modify_nullable else "NOT NULL")
        if op.modify_server_default is not False:
            changes.append("server default")
        if op.modify_comment is not False:
            changes.append("comment")
        return f"Alter column {op.table_name}.{op.column_name} ({', '.join(changes) or 'changed'})"
    if isinstance(op, ops.CreateIndexOp):
        return f"Create index {op.index_name} on {op.table_name}"
    if isinstance(op, ops.DropIndexOp):
        return f"Drop index {op.index_name}"
    if isinstance(op, ops.CreateUniqueConstraintOp):
        return f"Add unique constraint {op.constraint_name} on {op.table_name}"
    if isinstance(op, ops.CreateForeignKeyOp):
        return f"Add foreign key {op.constraint_name} on {op.source_table}"
    if isinstance(op, ops.DropConstraintOp):
        return f"Drop constraint {op.constraint_name} on {op.table_name}"
    if isinstance(op, ops.ExecuteSQLOp):
        return "Run SQL (data fix)"
    return type(op).__name__


def reverse_ops(operations: list[Any]) -> list[Any]:
    """Reverse a list of operations, including renames (Alembic's ``reverse()`` loses them)."""
    reversed_ops: list[Any] = []
    for op in reversed(operations):
        if isinstance(op, ops.ModifyTableOps):
            reversed_ops.append(ops.ModifyTableOps(op.table_name, reverse_ops(op.ops), schema=op.schema))
        elif isinstance(op, ops.RenameTableOp):
            reversed_ops.append(ops.RenameTableOp(op.new_table_name, op.table_name, schema=op.schema))
        elif isinstance(op, ops.AlterColumnOp) and op.modify_name:
            changed_null = op.modify_nullable is not None
            reversed_ops.append(
                ops.AlterColumnOp(
                    op.table_name,
                    op.modify_name,
                    schema=op.schema,
                    modify_name=op.column_name,
                    existing_type=op.existing_type,
                    existing_nullable=op.modify_nullable if changed_null else op.existing_nullable,
                    modify_nullable=op.existing_nullable if changed_null else None,
                )
            )
        else:
            reversed_ops.append(op.reverse())
    return reversed_ops


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def auto_name(upgrade_ops: Any, is_initial: bool) -> str:
    if is_initial:
        return "initial"
    all_ops = _walk(upgrade_ops)
    if len(all_ops) == 1:
        op = all_ops[0]
        if isinstance(op, ops.CreateTableOp):
            return f"create_{op.table_name}"
        if isinstance(op, ops.DropTableOp):
            return f"delete_{op.table_name}"
        if isinstance(op, ops.AddColumnOp):
            return f"{op.table_name}_{op.column.name}"
        if isinstance(op, ops.DropColumnOp):
            return f"remove_{op.table_name}_{op.column_name}"
        if isinstance(op, ops.AlterColumnOp) and op.modify_name:
            return f"rename_{op.table_name}_{op.column_name}_{op.modify_name}"
        if isinstance(op, ops.AlterColumnOp):
            return f"alter_{op.table_name}_{op.column_name}"
        if isinstance(op, ops.RenameTableOp):
            return f"rename_{op.table_name}_{op.new_table_name}"
        return _slug(describe(op))
    significant = [o for o in all_ops if not isinstance(o, (ops.CreateIndexOp, ops.DropIndexOp))]
    creates = [op.table_name for op in significant if isinstance(op, ops.CreateTableOp)]
    if creates and len(creates) == len(significant):
        return "create_" + (creates[0] if len(creates) == 1 else f"{creates[0]}_and_more")
    if len(significant) == 1:
        return auto_name(ops.UpgradeOps(ops=significant), False)
    first = describe(significant[0]) if significant else "changes"
    return _slug(first) + "_and_more"


def revision_id(number: int, name: str) -> str:
    """``0004_rename_events_location`` (cut at a word boundary to fit Alembic's 32 characters)."""
    rev = f"{number:04d}_{_slug(name)}"
    if len(rev) > MAX_REVISION_LENGTH:
        rev = rev[:MAX_REVISION_LENGTH]
        cut = rev.rfind("_")
        rev = rev[:cut] if cut > 5 else rev
    return rev.rstrip("_")


# ── Hook ────────────────────────────────────────────────────────────────────


class RevisionHook:
    """``process_revision_directives`` for makemigrations."""

    def __init__(self, plan: RevisionPlan, prompter: Prompter) -> None:
        self.plan = plan
        self.prompter = prompter

    def __call__(self, context: Any, revision: Any, directives: list[Any]) -> None:
        script = directives[0]
        upgrade_ops = script.upgrade_ops
        if not self.plan.empty and upgrade_ops.is_empty():
            self.plan.no_changes = True
            directives[:] = []
            return

        if not self.plan.empty:
            changed = self._handle_table_renames(upgrade_ops)
            changed |= self._handle_column_renames(upgrade_ops)
            if changed:
                script.downgrade_ops = ops.DowngradeOps(ops=reverse_ops(upgrade_ops.ops))
            self._check_destructive(upgrade_ops)
        # Named before _check_not_null adds its helper steps, which aren't what the user changed.
        name = self.plan.name or ("auto" if self.plan.empty else auto_name(upgrade_ops, self.plan.is_initial))
        if not self.plan.empty:
            self._check_not_null(context, upgrade_ops)

        script.rev_id = revision_id(self.plan.number, name)
        script.message = name.replace("_", " ")
        self.plan.revision = script.rev_id
        self.plan.operations = [describe(op) for op in _walk(upgrade_ops)]

    # Renames ────────────────────────────────────────────────────────────────

    def _handle_table_renames(self, upgrade_ops: Any) -> bool:
        drops = [op for op in upgrade_ops.ops if isinstance(op, ops.DropTableOp)]
        creates = [op for op in upgrade_ops.ops if isinstance(op, ops.CreateTableOp)]
        changed = False
        for drop in drops:
            for create in creates:
                if create.schema != drop.schema:
                    continue
                question = (
                    f"Was table {drop.table_name!r} renamed to {create.table_name!r} "
                    "(for example, did you rename the model class)?"
                )
                if not self.prompter.interactive:
                    self.plan.warnings.append(
                        f"Table {drop.table_name!r} is dropped and {create.table_name!r} created. If this is a "
                        "rename, run makemigrations interactively or set __tablename__ to keep the old name."
                    )
                    break
                if self.prompter.confirm(question):
                    index = upgrade_ops.ops.index(create)
                    upgrade_ops.ops[index] = ops.RenameTableOp(drop.table_name, create.table_name, schema=drop.schema)
                    upgrade_ops.ops.remove(drop)
                    creates.remove(create)
                    self.plan.warnings.append(
                        f"Only the rename of {drop.table_name!r} was recorded. Run `migrate`, then "
                        "`makemigrations` again to pick up any other changes to that table."
                    )
                    changed = True
                    break
        return changed

    def _handle_column_renames(self, upgrade_ops: Any) -> bool:
        changed = False
        for container in [op for op in upgrade_ops.ops if isinstance(op, ops.ModifyTableOps)]:
            drops = [op for op in container.ops if isinstance(op, ops.DropColumnOp)]
            adds = [op for op in container.ops if isinstance(op, ops.AddColumnOp)]
            renames = []
            for drop in drops:
                old = drop.to_column()
                for add in adds:
                    if add.column.type._type_affinity is not old.type._type_affinity:
                        continue
                    table = container.table_name
                    if not self.prompter.interactive:
                        self.plan.warnings.append(
                            f"{table}.{drop.column_name} is removed and {table}.{add.column.name} added. "
                            "If this is a rename, run makemigrations interactively."
                        )
                        break
                    if self.prompter.confirm(f"Was {table}.{drop.column_name} renamed to {table}.{add.column.name}?"):
                        rename = ops.AlterColumnOp(
                            table,
                            drop.column_name,
                            schema=container.schema,
                            modify_name=add.column.name,
                            existing_type=add.column.type,
                            existing_nullable=old.nullable,
                            modify_nullable=add.column.nullable if add.column.nullable != old.nullable else None,
                        )
                        container.ops.remove(drop)
                        container.ops.remove(add)
                        adds.remove(add)
                        renames.append(rename)
                        changed = True
                        break
            container.ops[0:0] = renames
        upgrade_ops.ops[:] = [op for op in upgrade_ops.ops if not (isinstance(op, ops.ModifyTableOps) and not op.ops)]
        return changed

    # Safety ─────────────────────────────────────────────────────────────────

    def _check_destructive(self, upgrade_ops: Any) -> None:
        destructive = []
        for op in _walk(upgrade_ops):
            if isinstance(op, ops.DropTableOp):
                destructive.append(
                    f"Drop table {op.table_name!r}: it is in the database but no model defines it. If you did "
                    "not delete that model, make sure its module is a models.py inside an app package."
                )
            elif isinstance(op, ops.DropColumnOp):
                destructive.append(f"Remove column {op.table_name}.{op.column_name}")
        if not destructive or self.plan.allow_destructive:
            return
        listing = "\n".join(f"  - {line}" for line in destructive)
        if not self.prompter.interactive:
            raise MigrationAborted(
                "These changes delete data:\n" + listing + "\nRe-run with --allow-destructive if that is intended."
            )
        if not self.prompter.confirm("These changes DELETE DATA:\n" + listing + "\nWrite this migration anyway?"):
            raise MigrationAborted("No migration written.")

    def _check_not_null(self, context: Any, upgrade_ops: Any) -> None:
        connection = getattr(context, "connection", None)
        if connection is None:
            return
        for container in list(upgrade_ops.ops):
            if not isinstance(container, ops.ModifyTableOps):
                continue
            table = container.table_name
            for op in list(container.ops):
                if isinstance(op, ops.AddColumnOp):
                    column = op.column
                    if column.nullable or column.server_default is not None:
                        continue
                    if self.plan.row_counts_known:
                        rows = _count(connection, table, container.schema)
                        if not rows:
                            continue
                        detail = f"{table} already has {rows} row(s)."
                    else:
                        detail = f"Existing rows in {table} need a value for it."
                    python_default = getattr(column, "default", None)
                    if python_default is not None and python_default.is_scalar:
                        # Like Django: fill existing rows with the field's default, no questions asked.
                        literal = _sql_literal(context, column, python_default.arg)
                    else:
                        literal = self._one_off_default(
                            context,
                            column,
                            f"Column {table}.{column.name} is NOT NULL with no database default. {detail}",
                        )
                    column.server_default = sa.schema.DefaultClause(sa.text(literal))
                    drop_default = ops.AlterColumnOp(
                        table,
                        column.name,
                        schema=container.schema,
                        modify_server_default=None,
                        existing_type=column.type,
                        existing_nullable=False,
                        existing_server_default=sa.text(literal),
                    )
                    # A separate container, so SQLite batch mode fills the rows before dropping the default.
                    position = upgrade_ops.ops.index(container) + 1
                    upgrade_ops.ops.insert(position, ops.ModifyTableOps(table, [drop_default], schema=container.schema))
                elif isinstance(op, ops.AlterColumnOp) and op.modify_nullable is False and op.existing_nullable:
                    if self.plan.row_counts_known:
                        nulls = _count(connection, table, container.schema, null_column=op.column_name)
                        if not nulls:
                            continue
                        detail = f"but {nulls} row(s) contain NULL."
                    else:
                        detail = "so existing NULL values need a replacement."
                    literal = self._one_off_default(
                        context,
                        sa.Column(op.column_name, op.existing_type or sa.String()),
                        f"{table}.{op.column_name} becomes NOT NULL, {detail}",
                    )
                    fix = f"UPDATE {_quote(context, table)} SET {_quote(context, op.column_name)} = {literal} " \
                          f"WHERE {_quote(context, op.column_name)} IS NULL"
                    upgrade_ops.ops.insert(upgrade_ops.ops.index(container), ops.ExecuteSQLOp(fix))

    def _one_off_default(self, context: Any, column: Any, problem: str) -> str:
        if not self.prompter.interactive:
            raise MigrationAborted(
                problem + "\nThe migration would fail. Give the field a default (default=... / "
                "server_default=...), make it nullable, or run makemigrations interactively."
            )
        answer = self.prompter.ask(
            problem + "\nEnter a one-off value for existing rows (a Python literal, e.g. 0, 'draft', True), "
            "or leave empty to quit"
        )
        if not answer.strip():
            raise MigrationAborted("No migration written. Add a default to the field and try again.")
        try:
            value = ast.literal_eval(answer)
        except (ValueError, SyntaxError):
            value = answer
        return _sql_literal(context, column, value)


def _sql_literal(context: Any, column: Any, value: Any) -> str:
    compiled = sa.literal(value, type_=column.type).compile(
        dialect=context.dialect, compile_kwargs={"literal_binds": True}
    )
    return str(compiled)


def _quote(context: Any, name: str) -> str:
    return context.dialect.identifier_preparer.quote(name)


def _count(connection: Any, table: str, schema: Optional[str], null_column: Optional[str] = None) -> int:
    target = sa.table(table, *( [sa.column(null_column)] if null_column else [] ), schema=schema)
    stmt = sa.select(sa.func.count()).select_from(target)
    if null_column:
        stmt = stmt.where(target.c[null_column].is_(None))
    try:
        # A savepoint, so a failed count cannot poison the surrounding transaction (Postgres).
        with connection.begin_nested():
            return int(connection.execute(stmt).scalar() or 0)
    except Exception:
        return 0
