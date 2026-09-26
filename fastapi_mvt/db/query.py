"""Django-style async QuerySet over SQLAlchemy ``select()``.

A QuerySet is an immutable query builder: every method returns a new one, and
nothing touches the database until you ``await`` a terminal method. The
statement underneath is always a plain SQLAlchemy ``Select``
(``qs.statement``), so any part of SQLAlchemy can be mixed in::

    posts = await (
        Post.objects
        .filter(published=True, author__name__icontains="ada")
        .filter(Post.views > 100)            # SQLAlchemy expressions work too
        .exclude(Q(title="") | Q(title=None))
        .prefetch("author", "tags")
        .order_by("-created_at")
    )
"""

from __future__ import annotations

import difflib
import math
from typing import TYPE_CHECKING, Any, AsyncIterator, Generator, Generic, Iterable, Optional, TypeVar

from sqlalchemy import and_, exists, extract, func, not_, or_, select, true
from sqlalchemy import delete as sa_delete
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import RelationshipProperty, joinedload, load_only, selectinload
from sqlalchemy.sql import ColumnElement, Select

from fastapi_mvt.db.database import session_scope
from fastapi_mvt.db.exceptions import FieldError

if TYPE_CHECKING:
    from fastapi_mvt.db.models import Base
    from fastapi_mvt.db.pagination import Page, PageParams

M = TypeVar("M", bound="Base")
QS = TypeVar("QS", bound="QuerySet[Any]")


# ── Q objects ───────────────────────────────────────────────────────────────


class Q:
    """Combine lookups with ``|`` (OR), ``&`` (AND) and ``~`` (NOT), as in Django."""

    AND = "AND"
    OR = "OR"

    def __init__(self, *children: Any, _connector: str = AND, _negated: bool = False, **lookups: Any) -> None:
        self.children: list[Any] = list(children) + sorted(lookups.items())
        self.connector = _connector
        self.negated = _negated

    def _combine(self, other: "Q", connector: str) -> "Q":
        if not isinstance(other, Q):
            raise TypeError(f"Cannot combine Q with {type(other).__name__}")
        return Q(self, other, _connector=connector)

    def __or__(self, other: "Q") -> "Q":
        return self._combine(other, self.OR)

    def __and__(self, other: "Q") -> "Q":
        return self._combine(other, self.AND)

    def __invert__(self) -> "Q":
        return Q(self, _negated=True)

    def __repr__(self) -> str:
        inner = f" {self.connector} ".join(repr(c) for c in self.children)
        return f"{'NOT ' if self.negated else ''}({inner})"


# ── Lookups ─────────────────────────────────────────────────────────────────


def _exact(col: Any, value: Any) -> Any:
    return col.is_(None) if value is None else col == value


def _in(col: Any, value: Any) -> Any:
    if isinstance(value, QuerySet):
        return col.in_(value._values_statement([value._single_value_column()]))
    return col.in_(list(value))


def _range(col: Any, value: Any) -> Any:
    low, high = value
    return col.between(low, high)


LOOKUPS: dict[str, Any] = {
    "exact": _exact,
    "iexact": lambda c, v: func.lower(c) == func.lower(v),
    "contains": lambda c, v: c.contains(v, autoescape=True),
    "icontains": lambda c, v: c.icontains(v, autoescape=True),
    "startswith": lambda c, v: c.startswith(v, autoescape=True),
    "istartswith": lambda c, v: c.istartswith(v, autoescape=True),
    "endswith": lambda c, v: c.endswith(v, autoescape=True),
    "iendswith": lambda c, v: c.iendswith(v, autoescape=True),
    "in": _in,
    "gt": lambda c, v: c > v,
    "gte": lambda c, v: c >= v,
    "lt": lambda c, v: c < v,
    "lte": lambda c, v: c <= v,
    "isnull": lambda c, v: c.is_(None) if v else c.is_not(None),
    "range": _range,
    "year": lambda c, v: extract("year", c) == v,
    "month": lambda c, v: extract("month", c) == v,
    "day": lambda c, v: extract("day", c) == v,
}
_RELATION_LOOKUPS = {"exact", "isnull", "in"}


def _suggest(model: Any, name: str) -> str:
    fields = sorted(sa_inspect(model).attrs.keys()) + ["pk"]
    close = difflib.get_close_matches(name, fields, n=1)
    hint = f" Did you mean {close[0]!r}?" if close else ""
    return f"{model.__name__} has no field {name!r}.{hint} Available fields: {', '.join(fields)}."


def _resolve_field(model: Any, name: str) -> Any:
    mapper = sa_inspect(model)
    if name == "pk":
        pk = mapper.primary_key
        if len(pk) != 1:
            raise FieldError(f"{model.__name__} has a composite primary key; 'pk' is ambiguous.")
        return mapper.get_property_by_column(pk[0])
    if name in mapper.attrs:
        return mapper.attrs[name]
    raise FieldError(_suggest(model, name))


def _column_condition(model: Any, prop: Any, lookup: str, value: Any, negated: bool) -> Any:
    if lookup not in LOOKUPS:
        close = difflib.get_close_matches(lookup, LOOKUPS, n=1)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        raise FieldError(
            f"Unsupported lookup {lookup!r} on {model.__name__}.{prop.key}.{hint} "
            f"Supported: {', '.join(LOOKUPS)}. For anything else pass a SQLAlchemy expression."
        )
    column = getattr(model, prop.key)
    condition = LOOKUPS[lookup](column, value)
    # Django semantics: exclude(x=1) keeps rows where x IS NULL.
    if negated and lookup != "isnull" and value is not None and any(c.nullable for c in prop.columns):
        condition = and_(condition, column.is_not(None))
    return condition


def _relation_condition(model: Any, prop: RelationshipProperty, lookup: str, value: Any) -> Any:
    attr = getattr(model, prop.key)
    if lookup == "isnull":
        present = attr.any() if prop.uselist else attr.has()
        return not_(present) if value else present
    if lookup == "in":
        pk = sa_inspect(prop.mapper.class_).primary_key[0]
        ids = [getattr(v, pk.key, v) for v in value]
        target = prop.mapper.class_
        inner = getattr(target, pk.key).in_(ids)
        return attr.any(inner) if prop.uselist else attr.has(inner)
    if value is None:
        return not_(attr.any()) if prop.uselist else attr.is_(None)
    return attr.contains(value) if prop.uselist else attr == value


def build_conditions(model: Any, lookups: Iterable[tuple[str, Any]], negated: bool = False) -> list[Any]:
    """Compile ``field__subfield__lookup=value`` pairs into SQLAlchemy conditions.

    Lookups through the same multi-valued relation inside one call are grouped
    into a single EXISTS, so ``filter(tags__name="a", tags__active=True)``
    means "has one tag that is both", as in Django.
    """
    conditions: list[Any] = []
    related: dict[str, list[tuple[str, Any]]] = {}

    for key, value in lookups:
        parts = key.split("__")
        prop = _resolve_field(model, parts[0])
        rest = parts[1:]

        if isinstance(prop, RelationshipProperty):
            target = prop.mapper.class_
            target_attrs = sa_inspect(target).attrs
            if not rest or (len(rest) == 1 and rest[0] in _RELATION_LOOKUPS and rest[0] not in target_attrs):
                conditions.append(_relation_condition(model, prop, rest[0] if rest else "exact", value))
            else:
                related.setdefault(prop.key, []).append(("__".join(rest), value))
            continue

        if len(rest) > 1:
            raise FieldError(
                f"{model.__name__}.{prop.key} is a column, so {'__'.join(rest)!r} cannot follow it."
            )
        if rest and rest[0] not in LOOKUPS and rest[0] in sa_inspect(model).attrs:
            raise FieldError(f"{model.__name__}.{prop.key} is not a relation; cannot traverse to {rest[0]!r}.")
        conditions.append(_column_condition(model, prop, rest[0] if rest else "exact", value, negated))

    for rel_name, sub_lookups in related.items():
        prop = sa_inspect(model).attrs[rel_name]
        inner = and_(*build_conditions(prop.mapper.class_, sub_lookups, negated))
        attr = getattr(model, rel_name)
        conditions.append(attr.any(inner) if prop.uselist else attr.has(inner))
    return conditions


def _compile_q(model: Any, q: Q, negated: bool = False) -> Any:
    parts: list[Any] = []
    lookups: list[tuple[str, Any]] = []
    for child in q.children:
        if isinstance(child, Q):
            parts.append(_compile_q(model, child, negated != child.negated))
        elif isinstance(child, tuple):
            lookups.append(child)
        else:
            parts.append(child)
    parts.extend(build_conditions(model, lookups, negated=negated != q.negated) if lookups else [])
    joined = or_(*parts) if q.connector == Q.OR else and_(*parts)
    return not_(joined) if q.negated else joined


# ── QuerySet ────────────────────────────────────────────────────────────────


class QuerySet(Generic[M]):
    def __init__(self, model: type[M]) -> None:
        self.model = model
        self._where: list[Any] = []
        self._order: list[Any] = []
        self._options: list[Any] = []
        self._limit: Optional[int] = None
        self._offset: Optional[int] = None
        self._distinct = False
        self._for_update = False
        self._session: Optional[AsyncSession] = None

    def _clone(self: QS) -> QS:
        clone = self.__class__.__new__(self.__class__)
        clone.__dict__.update(self.__dict__)
        clone._where = list(self._where)
        clone._order = list(self._order)
        clone._options = list(self._options)
        return clone

    # ── Builders ────────────────────────────────────────────────────────────

    def _conditions(self, args: tuple[Any, ...], kwargs: dict[str, Any], negated: bool) -> list[Any]:
        conditions: list[Any] = []
        for arg in args:
            if isinstance(arg, Q):
                conditions.append(_compile_q(self.model, arg, negated))
            elif isinstance(arg, bool):
                raise TypeError(
                    "filter() received a plain bool. Did you write `Model.field == value` "
                    "on an instance, or `field is None`? Use keyword lookups or `Model.field.is_(None)`."
                )
            else:
                conditions.append(arg)
        conditions.extend(build_conditions(self.model, kwargs.items(), negated=negated))
        return conditions

    def filter(self: QS, *expressions: Any, **lookups: Any) -> QS:
        """Keep rows matching all conditions: ``filter(status="open", price__lt=10, Post.views > 5)``."""
        clone = self._clone()
        clone._where.extend(self._conditions(expressions, lookups, negated=False))
        return clone

    def exclude(self: QS, *expressions: Any, **lookups: Any) -> QS:
        """Drop rows matching all given conditions."""
        conditions = self._conditions(expressions, lookups, negated=True)
        clone = self._clone()
        if conditions:
            clone._where.append(not_(and_(*conditions)))
        return clone

    def order_by(self: QS, *fields: Any) -> QS:
        """``order_by("-created_at", "title")``. Calling it replaces any previous ordering."""
        clone = self._clone()
        clone._order = []
        for field in fields:
            if isinstance(field, str):
                descending = field.startswith("-")
                name = field.lstrip("-")
                prop = _resolve_field(self.model, name)
                if isinstance(prop, RelationshipProperty):
                    raise FieldError(f"Cannot order by relation {name!r}; order by a column such as '{name}_id'.")
                column = getattr(self.model, prop.key)
                clone._order.append(column.desc() if descending else column.asc())
            else:
                clone._order.append(field)
        return clone

    def _load_paths(self, paths: tuple[str, ...], loader: Any) -> list[Any]:
        options = []
        for path in paths:
            model = self.model
            option = None
            for name in path.split("__"):
                prop = _resolve_field(model, name)
                if not isinstance(prop, RelationshipProperty):
                    raise FieldError(f"{model.__name__}.{name} is not a relation, so it cannot be prefetched.")
                attr = getattr(model, name)
                option = loader(attr) if option is None else getattr(option, loader.__name__)(attr)
                model = prop.mapper.class_
            options.append(option)
        return options

    def prefetch(self: QS, *relations: str) -> QS:
        """Load relations with one extra query each (``selectinload``); ``"author__profile"`` nests."""
        clone = self._clone()
        clone._options.extend(self._load_paths(relations, selectinload))
        return clone

    prefetch_related = prefetch

    def select_related(self: QS, *relations: str) -> QS:
        """Load to-one relations in the same query with a JOIN (``joinedload``)."""
        clone = self._clone()
        clone._options.extend(self._load_paths(relations, joinedload))
        return clone

    def only(self: QS, *fields: str) -> QS:
        clone = self._clone()
        clone._options.append(load_only(*(getattr(self.model, _resolve_field(self.model, f).key) for f in fields)))
        return clone

    def options(self: QS, *options: Any) -> QS:
        """Add raw SQLAlchemy loader options."""
        clone = self._clone()
        clone._options.extend(options)
        return clone

    def distinct(self: QS) -> QS:
        clone = self._clone()
        clone._distinct = True
        return clone

    def limit(self: QS, count: Optional[int]) -> QS:
        clone = self._clone()
        clone._limit = count
        return clone

    def offset(self: QS, count: Optional[int]) -> QS:
        clone = self._clone()
        clone._offset = count
        return clone

    def select_for_update(self: QS) -> QS:
        """Lock the selected rows until the transaction ends (ignored by SQLite)."""
        clone = self._clone()
        clone._for_update = True
        return clone

    def using(self: QS, session: AsyncSession) -> QS:
        """Run against an explicit ``AsyncSession`` instead of the ambient one."""
        clone = self._clone()
        clone._session = session
        return clone

    def __getitem__(self: QS, item: slice) -> QS:
        if not isinstance(item, slice):
            raise TypeError(
                "QuerySets only support slices, e.g. qs[:10]. For one row use "
                "`await qs.offset(n).first()`."
            )
        if item.step not in (None, 1):
            raise ValueError("Slice steps are not supported.")
        start = item.start or 0
        if start < 0 or (item.stop is not None and item.stop < 0):
            raise ValueError("Negative indexing is not supported.")
        clone = self.offset(start or None)
        if item.stop is not None:
            clone._limit = max(item.stop - start, 0)
        return clone

    # ── Statements ──────────────────────────────────────────────────────────

    @property
    def statement(self) -> Select:
        """The SQLAlchemy ``Select`` this QuerySet runs."""
        stmt = select(self.model)
        if self._where:
            stmt = stmt.where(*self._where)
        if self._order:
            stmt = stmt.order_by(*self._order)
        if self._options:
            stmt = stmt.options(*self._options)
        if self._distinct:
            stmt = stmt.distinct()
        if self._limit is not None:
            stmt = stmt.limit(self._limit)
        if self._offset is not None:
            stmt = stmt.offset(self._offset)
        if self._for_update:
            stmt = stmt.with_for_update()
        return stmt

    def sql(self) -> str:
        """The SQL as text, for debugging."""
        try:
            return str(self.statement.compile(compile_kwargs={"literal_binds": True}))
        except Exception:
            return str(self.statement)

    def __repr__(self) -> str:
        return f"<QuerySet {self.model.__name__}: {self.sql()}>"

    def _values_statement(self, columns: list[Any]) -> Select:
        # select_from: without it, exists()/aggregates with no filter would have no FROM clause.
        stmt = select(*columns).select_from(self.model)
        if self._where:
            stmt = stmt.where(*self._where)
        if self._order:
            stmt = stmt.order_by(*self._order)
        if self._distinct:
            stmt = stmt.distinct()
        if self._limit is not None:
            stmt = stmt.limit(self._limit)
        if self._offset is not None:
            stmt = stmt.offset(self._offset)
        return stmt

    def _single_value_column(self) -> Any:
        return sa_inspect(self.model).primary_key[0]

    def _columns(self, fields: tuple[str, ...]) -> list[Any]:
        if not fields:
            return [getattr(self.model, a.key) for a in sa_inspect(self.model).column_attrs]
        columns = []
        for name in fields:
            prop = _resolve_field(self.model, name)
            if isinstance(prop, RelationshipProperty):
                raise FieldError(f"values() takes columns; {name!r} is a relation (use '{name}_id').")
            columns.append(getattr(self.model, prop.key).label(name))
        return columns

    # ── Reading ─────────────────────────────────────────────────────────────

    def all(self: QS) -> QS:
        """A copy of this QuerySet, as in Django. ``await Post.objects.all()`` gives a list."""
        return self._clone()

    async def _fetch(self) -> list[M]:
        async with session_scope(self._session) as s:
            result = await s.execute(self.statement)
            return list(result.unique().scalars().all())

    def __await__(self) -> Generator[Any, None, list[M]]:
        """Awaiting a QuerySet runs it: ``posts = await Post.objects.filter(...)``."""
        return self._fetch().__await__()

    async def __aiter__(self) -> AsyncIterator[M]:
        for obj in await self._fetch():
            yield obj

    async def first(self) -> Optional[M]:
        qs = self if self._order else self.order_by(*sa_inspect(self.model).primary_key)
        rows = await qs.limit(1)._fetch()
        return rows[0] if rows else None

    async def last(self) -> Optional[M]:
        if self._order:
            reversed_order = [_reverse(o) for o in self._order]
            qs = self._clone()
            qs._order = reversed_order
        else:
            qs = self.order_by(*(c.desc() for c in sa_inspect(self.model).primary_key))
        rows = await qs.limit(1)._fetch()
        return rows[0] if rows else None

    async def get(self, *expressions: Any, **lookups: Any) -> M:
        """Exactly one row, or raise ``Model.DoesNotExist`` (-> 404) / ``MultipleObjectsReturned``."""
        qs = self.filter(*expressions, **lookups) if expressions or lookups else self
        rows = await qs.limit(2)._fetch()
        if not rows:
            raise self.model.DoesNotExist(f"{self.model.__name__} matching query does not exist.")
        if len(rows) > 1:
            raise self.model.MultipleObjectsReturned(
                f"get() returned more than one {self.model.__name__}; add more filters."
            )
        return rows[0]

    async def get_or_none(self, *expressions: Any, **lookups: Any) -> Optional[M]:
        try:
            return await self.get(*expressions, **lookups)
        except self.model.DoesNotExist:
            return None

    async def count(self) -> int:
        inner = self._values_statement([self._single_value_column()]).order_by(None).subquery()
        async with session_scope(self._session) as s:
            return int((await s.execute(select(func.count()).select_from(inner))).scalar_one())

    async def exists(self) -> bool:
        stmt = self._values_statement([true()]).order_by(None).limit(1)
        async with session_scope(self._session) as s:
            return bool((await s.execute(select(exists(stmt)))).scalar())

    async def values(self, *fields: str) -> list[dict[str, Any]]:
        """Rows as dicts: ``await Post.objects.values("id", "title")``."""
        async with session_scope(self._session) as s:
            result = await s.execute(self._values_statement(self._columns(fields)))
            return [dict(row) for row in result.mappings().all()]

    async def values_list(self, *fields: str, flat: bool = False) -> list[Any]:
        if flat and len(fields) != 1:
            raise TypeError("values_list(flat=True) takes exactly one field.")
        async with session_scope(self._session) as s:
            result = await s.execute(self._values_statement(self._columns(fields)))
            return list(result.scalars().all()) if flat else [tuple(r) for r in result.all()]

    async def in_bulk(self, ids: Iterable[Any]) -> dict[Any, M]:
        pk = self._single_value_column()
        objects = await self.filter(getattr(self.model, pk.key).in_(list(ids)))._fetch()
        return {getattr(obj, pk.key): obj for obj in objects}

    async def aggregate(self, **expressions: Any) -> dict[str, Any]:
        """``await Order.objects.filter(paid=True).aggregate(total=func.sum(Order.amount))``."""
        stmt = select(*(expr.label(name) for name, expr in expressions.items())).select_from(self.model)
        if self._where:
            stmt = stmt.where(*self._where)
        async with session_scope(self._session) as s:
            return dict((await s.execute(stmt)).mappings().one())

    async def paginate(self, params: Optional["PageParams"] = None, *, page: int = 1, size: int = 20) -> "Page[M]":
        """One page of results plus totals, ready to return as ``Page[Schema]``."""
        from fastapi_mvt.db.pagination import Page

        if params is not None:
            page, size = params.page, params.size
        page, size = max(page, 1), max(size, 1)
        qs = self if self._order else self.order_by(*sa_inspect(self.model).primary_key)
        total = await qs.count()
        items = await qs.offset((page - 1) * size).limit(size)._fetch()
        return Page(items=items, total=total, page=page, size=size, pages=math.ceil(total / size) if total else 0)

    # ── Writing ─────────────────────────────────────────────────────────────

    def _check_bulk(self, operation: str) -> None:
        if self._limit is not None or self._offset is not None:
            raise TypeError(f"Cannot {operation}() a sliced QuerySet.")

    async def update(self, **values: Any) -> int:
        """Bulk UPDATE; returns the number of rows. ``values`` may use expressions: ``views=Post.views + 1``."""
        self._check_bulk("update")
        mapper = sa_inspect(self.model)
        for key in values:
            if key not in mapper.column_attrs:
                raise FieldError(_suggest(self.model, key) if key not in mapper.attrs else
                                 f"update() sets columns; {key!r} is a relation (use '{key}_id').")
        stmt = sa_update(self.model).where(*self._where).values(**values)
        async with session_scope(self._session) as s:
            result = await s.execute(stmt.execution_options(synchronize_session="fetch"))
            return int(result.rowcount)  # type: ignore[attr-defined]

    async def delete(self) -> int:
        """Bulk DELETE; returns the number of rows. Related rows follow their ``on_delete`` rule."""
        self._check_bulk("delete")
        stmt = sa_delete(self.model).where(*self._where)
        async with session_scope(self._session) as s:
            result = await s.execute(stmt.execution_options(synchronize_session="fetch"))
            return int(result.rowcount)  # type: ignore[attr-defined]

    def _new(self, fields: dict[str, Any]) -> M:
        # A None for a NOT NULL column that has a default means "use the default"
        # (typical when passing schema.model_dump() straight in).
        mapper = sa_inspect(self.model)
        cleaned = {}
        for key, value in fields.items():
            if value is None and key in mapper.column_attrs:
                column = mapper.column_attrs[key].columns[0]
                if not column.nullable and (column.default is not None or column.server_default is not None):
                    continue
            cleaned[key] = value
        return self.model(**cleaned)

    async def create(self, **fields: Any) -> M:
        """INSERT one row and return it with its id and defaults filled in."""
        obj = self._new(fields)
        async with session_scope(self._session) as s:
            s.add(obj)
            await s.flush()
        return obj

    async def bulk_create(self, objects: Iterable[M]) -> list[M]:
        objects = list(objects)
        async with session_scope(self._session) as s:
            s.add_all(objects)
            await s.flush()
        return objects

    def _plain_lookups(self, lookups: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in lookups.items() if "__" not in k}

    async def get_or_create(self, defaults: Optional[dict[str, Any]] = None, **lookups: Any) -> tuple[M, bool]:
        """Return ``(obj, created)``. Safe under concurrency when a unique constraint covers the lookups."""
        async with session_scope(self._session) as s:
            qs = self.using(s)
            obj = await qs.get_or_none(**lookups)
            if obj is not None:
                return obj, False
            try:
                async with s.begin_nested():
                    obj = self._new({**self._plain_lookups(lookups), **(defaults or {})})
                    s.add(obj)
                    await s.flush()
                return obj, True
            except IntegrityError:
                # Someone else inserted it between our SELECT and INSERT.
                existing = await qs.get_or_none(**lookups)
                if existing is None:
                    raise
                return existing, False

    async def update_or_create(self, defaults: Optional[dict[str, Any]] = None, **lookups: Any) -> tuple[M, bool]:
        """Update the matching row with *defaults*, or create it. Returns ``(obj, created)``."""
        async with session_scope(self._session) as s:
            qs = self.using(s)
            obj = await qs.select_for_update().get_or_none(**lookups)
            if obj is None:
                obj, created = await qs.get_or_create(defaults=defaults, **lookups)
                if created:
                    return obj, True
            for key, value in (defaults or {}).items():
                setattr(obj, key, value)
            await s.flush()
            return obj, False


def _reverse(clause: Any) -> Any:
    modifier = getattr(clause, "modifier", None)
    element = getattr(clause, "element", clause)
    from sqlalchemy.sql import operators

    if modifier is operators.desc_op:
        return element.asc()
    if modifier is operators.asc_op:
        return element.desc()
    return clause.desc() if isinstance(clause, ColumnElement) else clause
