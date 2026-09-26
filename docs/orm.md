# ORM guide

fastapi-mvt models are **SQLAlchemy 2 models** with a Django-style API on top.
You get Django's everyday ergonomics without giving anything up: every model is
a real SQLAlchemy mapped class, and every QuerySet is a real SQLAlchemy `select()`.

```python
from fastapi_mvt.db import Q, atomic, on_commit
```

- [Defining models](#defining-models)
- [Relationships](#relationships)
- [Querying](#querying)
- [Creating, updating, deleting](#creating-updating-deleting)
- [Transactions](#transactions)
- [Schemas](#schemas)
- [Pagination](#pagination)
- [Custom QuerySets](#custom-querysets)
- [Using SQLAlchemy directly](#using-sqlalchemy-directly)
- [Sync code](#sync-code)
- [Where sessions come from](#where-sessions-come-from)
- [Common errors](#common-errors)

---

## Defining models

```python
from sqlalchemy import Text

from fastapi_mvt.db import (
    CASCADE, BooleanField, CharField, DateField, DecimalField, ForeignKey,
    IntegerField, JSONField, SlugField, TextField, TimestampedModel, private,
)


class Product(TimestampedModel):
    name = CharField(max_length=200, index=True)
    slug = SlugField(unique=True)
    description = TextField(null=True)
    price = DecimalField(max_digits=10, decimal_places=2)
    stock = IntegerField(default=0)
    in_stock = BooleanField(default=True)
    released = DateField(null=True)
    extra = JSONField(default=dict)
    category_id = ForeignKey("Category", on_delete=CASCADE)
    internal_notes = private(Text, nullable=True)   # never appears in API schemas or the admin
```

| Base class | Adds |
|---|---|
| `Model` | `id: int` primary key |
| `TimestampedModel` | `id`, `created_at`, `updated_at` (set automatically, UTC) |
| `Base` | nothing (bring your own primary key) |

### Field helpers

As in Django, every field is required (NOT NULL) unless you pass `null=True`.

| Field | Column | Python type |
|---|---|---|
| `CharField(max_length=n)` | VARCHAR(n); `n` also limits API input | `str` |
| `TextField()` | TEXT | `str` |
| `EmailField()`, `SlugField()` | VARCHAR(254) / VARCHAR(50, indexed) | `str` |
| `IntegerField()`, `BigIntegerField()` | INTEGER / BIGINT | `int` |
| `FloatField()` | FLOAT | `float` |
| `DecimalField(max_digits, decimal_places)` | NUMERIC | `Decimal` |
| `BooleanField(default=False)` | BOOLEAN | `bool` |
| `DateTimeField(auto_now_add=True / auto_now=True)` | timestamp, always UTC-aware | `datetime` |
| `DateField()`, `TimeField()` | DATE / TIME | `date` / `time` |
| `UUIDField(default=uuid.uuid4)` | UUID | `UUID` |
| `JSONField(default=dict)` | JSON | any |
| `EnumField(MyEnum, default=MyEnum.X)` | VARCHAR holding the value (Django `choices`) | `MyEnum` |
| `ForeignKey(Model, on_delete=..., null=False)` | the `<name>_id` column | `int` |

Common options: `null`, `default` (a value or a callable), `unique`, `index`,
`db_default` (a default written into the database schema), `help_text`.

### The SQLAlchemy spelling

Each helper is a plain SQLAlchemy `mapped_column`, so this is identical, and the
two styles mix freely:

```python
from sqlalchemy import String
from fastapi_mvt.db import Mapped, mapped_column

class Product(TimestampedModel):
    name: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str | None]
```

Use it for anything the helpers don't cover (`Computed`, custom types, database-specific columns).

**Table names** are derived from the class (`OrderItem` → `order_items`,
`Category` → `categories`). Set `__tablename__ = "..."` to choose.

**Constraints and indexes**:

```python
from sqlalchemy import UniqueConstraint

class Membership(Model):
    __table_args__ = (UniqueConstraint("group_id", "user_id"),)
```

Constraint names are generated deterministically, which migrations need.

---

## Relationships

```python
from fastapi_mvt.db import CASCADE, SET_NULL, CharField, ForeignKey, Mapped, relationship


class Author(TimestampedModel):
    name = CharField(max_length=100)
    posts: Mapped[list["Post"]] = relationship(back_populates="author", foreign_keys="Post.author_id")


class Post(TimestampedModel):
    title = CharField(max_length=200)
    author_id = ForeignKey(Author, on_delete=CASCADE)
    editor_id = ForeignKey("Author", on_delete=SET_NULL, null=True)
    author: Mapped[Author] = relationship(back_populates="posts", foreign_keys=[author_id])
    tags: Mapped[list["Tag"]] = relationship(secondary="post_tags")
```

**`ForeignKey(target, on_delete=...)`** creates the foreign-key *column*
(`author_id`); the Python-side link is a separate `relationship()`. `target` is
a model class, a model name (`"Author"`), or `"table.column"`. `on_delete` is
required (and `SET_NULL` needs `null=True`), as in Django. With the SQLAlchemy
spelling use `author_id: Mapped[int] = fk(Author, on_delete=CASCADE)`.

| `on_delete` | When the referenced row is deleted |
|---|---|
| `CASCADE` | delete this row too |
| `SET_NULL` | set the column to NULL |
| `PROTECT` / `RESTRICT` | refuse the delete (the database raises; the API returns 409) |
| `NO_ACTION`, `SET_DEFAULT` | as in SQL |

The **database** enforces these rules, including on SQLite, where fastapi-mvt
turns foreign keys on. That makes them correct for bulk deletes too.

**`relationship()`** is SQLAlchemy's, with one change: it never loads data
implicitly. In async code, implicit loading can't work, and even in sync code it
causes N+1 query storms. Load relations explicitly:

```python
posts = await Post.objects.prefetch("author", "tags")      # one extra query per relation
post = await Post.objects.select_related("author").get(id=1)  # JOIN, for to-one relations
await post.load("tags")                                     # on an object you already have
comments = await Comment.objects.prefetch("post__author")   # nested
```

Reading a relation you didn't load raises `RelationNotLoaded` with the fix:

```
Post.author is not loaded. Load it with the query: .prefetch("author") (or .select_related("author")),
or on this object: await obj.load("author").
```

So a missing `prefetch` shows up in development, never as a hidden query per row in production.

Many-to-many uses a plain association table:

```python
import sqlalchemy as sa
from fastapi_mvt.db import Base

post_tags = sa.Table(
    "post_tags", Base.metadata,
    sa.Column("post_id", sa.ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("tag_id", sa.ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)
```

---

## Querying

`Model.objects` returns a **QuerySet**. It's immutable, so every method returns a
new one, and it hits the database only when awaited.

```python
posts = await Post.objects.filter(published=True).order_by("-created_at")[:10]
```

### Methods that build a query

| Method | Example |
|---|---|
| `filter(**lookups, *expressions)` | `.filter(status="open", price__lt=10)` |
| `exclude(...)` | `.exclude(title="")` (keeps NULL rows, as in Django) |
| `order_by(*fields)` | `.order_by("-created_at", "title")` |
| `[start:stop]` | `[20:40]` → OFFSET 20 LIMIT 20 |
| `limit(n)` / `offset(n)` | |
| `prefetch(*relations)` / `prefetch_related` | `.prefetch("author", "tags__owner")` |
| `select_related(*relations)` | `.select_related("author")` |
| `only(*fields)` | load just some columns |
| `distinct()` | |
| `select_for_update()` | lock rows until the transaction ends (PostgreSQL/MySQL) |
| `all()` | a copy (so `Post.objects.all().filter(...)` works) |
| `using(session)` | run on an explicit `AsyncSession` |
| `options(...)` | raw SQLAlchemy loader options |

### Lookups

`field__lookup=value`; without a lookup it means `exact`.

| Lookup | SQL |
|---|---|
| `exact` (`field=None` → IS NULL) | `=` |
| `iexact` | case-insensitive `=` |
| `contains`, `icontains` | `LIKE '%v%'` (wildcards in `v` are escaped) |
| `startswith`, `istartswith`, `endswith`, `iendswith` | |
| `in` | `IN (...)`, also accepts a QuerySet (subquery) |
| `gt`, `gte`, `lt`, `lte` | `>`, `>=`, `<`, `<=` |
| `range` | `BETWEEN a AND b` |
| `isnull` | `IS NULL` / `IS NOT NULL` |
| `year`, `month`, `day` | parts of a date/datetime |

Lookups **span relations** with `__`:

```python
await Post.objects.filter(author__name="Ada")              # many-to-one
await Author.objects.filter(posts__title__icontains="sql")  # reverse / one-to-many
await Post.objects.filter(tags__name="python", tags__active=True)  # one tag matching both
await Post.objects.filter(author=ada)                      # compare with an object
await Post.objects.filter(tags__isnull=True)               # posts without tags
```

A typo is caught with a suggestion:
`FieldError: Post has no field 'titel'. Did you mean 'title'?`

**OR / NOT** with `Q`:

```python
await Post.objects.filter(Q(views__gt=100) | Q(pinned=True), ~Q(status="draft"))
```

**Anything else** is a SQLAlchemy expression, passed positionally:

```python
from sqlalchemy import func
await Post.objects.filter(func.length(Post.title) > 50, Post.views > Post.likes)
```

### Methods that run the query (await them)

| Method | Returns |
|---|---|
| `await qs` | `list[Model]` |
| `async for obj in qs` | iterate |
| `get(**lookups)` | one object; raises `Model.DoesNotExist` (→ 404) or `Model.MultipleObjectsReturned` |
| `get_or_none(**lookups)` | object or `None` |
| `first()` / `last()` | object or `None` (ordered by `id` if unordered) |
| `count()` / `exists()` | `int` / `bool` |
| `values("id", "title")` | `list[dict]` |
| `values_list("id", flat=True)` | `list` |
| `in_bulk([1, 2, 3])` | `{id: obj}` |
| `aggregate(total=func.sum(Order.amount))` | `{"total": ...}` |
| `paginate(params)` | `Page` |

---

## Creating, updating, deleting

```python
post = await Post.objects.create(title="Hello", author_id=1)   # INSERT, returns it with id
post = Post(title="Hello", author=author); await post.save()    # same thing

await post.update(title="Hi", published=True)   # set + save
post.title = "Hey"; await post.save()
await post.refresh()                             # reload from the database
await post.delete()                              # related rows follow on_delete

await Post.objects.bulk_create([Post(...), Post(...)])
await Post.objects.filter(author_id=1).update(views=Post.views + 1)   # bulk UPDATE, returns count
await Post.objects.filter(status="spam").delete()                      # bulk DELETE, returns count

tag, created = await Tag.objects.get_or_create(name="python", defaults={"color": "blue"})
obj, created = await Setting.objects.update_or_create(key="theme", defaults={"value": "dark"})
```

`get_or_create` is safe when two requests race, provided a unique constraint
covers the lookup fields: the loser finds the winner's row instead of failing.

Passing `None` for a NOT NULL field that has a default uses the default, so
`create(**schema.model_dump())` works even when optional fields are omitted.

---

## Transactions

The rules:

1. **HTTP requests** (with `db.init_app(app)`): one transaction per request. It
   commits right before the response is sent; it rolls back if the endpoint
   raises (any exception, including `HTTPException`) or returns a status ≥ 400.
2. **`async with atomic():`** makes a block atomic. Nested inside a request or
   another `atomic()`, it's a savepoint: an error undoes only that block.
3. **Anywhere else** (shell, scripts, background tasks, WebSockets), each ORM call
   commits on its own, like Django's autocommit.

```python
async with atomic():
    order = await Order.objects.create(user_id=user.id)
    for item in cart:
        await OrderLine.objects.create(order_id=order.id, product_id=item.id)
        updated = await Product.objects.filter(id=item.id, stock__gte=1).update(stock=Product.stock - 1)
        if not updated:
            raise OutOfStock(item.id)      # everything in the block is rolled back
```

**`on_commit(callback)`** runs a sync or async callback after the outermost
transaction commits, and never if it rolls back. Use it for emails, task queues
and websocket pushes:

```python
post = await Post.objects.create(...)
on_commit(lambda: send_notification.delay(post.id))
```

Callbacks registered in a savepoint that rolls back are discarded. A failing
callback is logged, and doesn't turn a committed request into an error.

**Keeping writes on an error response.** An error response (status ≥ 400) rolls
the request back, which is what you want almost always. For a write that must
survive it (an audit entry, a failed-attempt counter), call `commit_on_error()`
first. Unhandled exceptions still roll back:

```python
from fastapi_mvt.db import commit_on_error

commit_on_error()
await AuditEntry.objects.create(user_id=user.id, action="denied")
raise HTTPException(403)
```

To turn the rule off for a whole app: `db.init_app(app, rollback_on_error_status=False)`.

**Celery tasks and scripts** can run an async function in one transaction with `db.run()`:

```python
@celery_app.task
def close_expired_events():
    db.run(_close_expired)

async def _close_expired():
    await Event.objects.filter(ends_at__lt=utcnow()).update(open=False)
```

---

## Schemas

Models describe tables; schemas describe your API. `ModelSchema` builds a
pydantic model from a model's columns so you don't write each field twice:

```python
from pydantic import Field
from fastapi_mvt.db import ModelSchema, Schema

class ProductCreate(ModelSchema, model=Product, mode="create"):
    name: str = Field(min_length=3, max_length=200)   # declared fields win

class ProductUpdate(ModelSchema, model=Product, mode="update"):
    pass

class ProductRead(ModelSchema, model=Product):         # "read" is the default
    category: CategoryRead                               # add nested/extra fields

@router.patch("/{id}", response_model=ProductRead)
async def update(id: int, data: ProductUpdate):
    product = await Product.objects.get(id=id)
    return await product.update(**data.model_dump(exclude_unset=True))
```

| Mode | Fields |
|---|---|
| `read` | all public columns |
| `create` | no auto primary key, no `created_at`/`updated_at`; fields with a default are optional |
| `update` | like `create`, all optional |

Also `include=[...]` and `exclude=[...]`. `CharField(max_length=n)` becomes
`max_length=n`, nullable fields become optional, and `private()` columns never
appear. Type checkers understand this class form. For a one-off,
`ProductRead = model_schema(Product)` does the same in one call, and
hand-written `Schema` subclasses (pydantic with `from_attributes=True`) work everywhere.

---

## Pagination

```python
from fastapi_mvt.db import Page, PageParams

@router.get("", response_model=Page[ProductRead])
async def list_products(params: PageParams = Depends()):
    return await Product.objects.order_by("name").paginate(params)
```

`?page=2&size=50` (size ≤ 100) returns
`{"items": [...], "total": 312, "page": 2, "size": 50, "pages": 7, "has_next": true, "has_previous": true}`.

---

## Custom QuerySets

Reusable query logic, like Django managers:

```python
from typing import ClassVar
from fastapi_mvt.db import Manager, QuerySet

class PostQuerySet(QuerySet["Post"]):
    def published(self):
        return self.filter(published=True)

    def by(self, author):
        return self.filter(author=author)

class Post(TimestampedModel):
    objects: ClassVar[Manager[PostQuerySet]] = Manager(PostQuerySet)
    ...

await Post.objects.published().by(ada).order_by("-created_at")
```

---

## Using SQLAlchemy directly

Nothing is hidden. Mix freely:

```python
from sqlalchemy import func, select
from fastapi_mvt.db import current_session

qs = Post.objects.filter(published=True)
qs.statement                 # the sqlalchemy Select
qs.sql()                     # the SQL text, for debugging

session = current_session()  # the request's AsyncSession
rows = await session.execute(
    select(Author.name, func.count(Post.id)).join(Post).group_by(Author.name)
)
```

Window functions, CTEs, `INSERT ... ON CONFLICT`, database-specific types: use
SQLAlchemy as documented. The models are the same objects.

`db.sessionmaker` is an `async_sessionmaker` for libraries that want one
(SQLAdmin, fastapi-users, ...), and it respects test isolation.

---

## Sync code

The ORM is async, but plain `def` code can use it through `async_to_sync`, named
after Django's `asgiref` helper:

```python
from fastapi_mvt.db import async_to_sync

@router.get("/legacy")
def legacy_endpoint():                                     # a plain `def` endpoint
    posts = async_to_sync(Post.objects.filter(published=True))
    return {"count": len(posts)}

save = async_to_sync(post.save)                            # or wrap an async function
save()
```

Inside a FastAPI `def` endpoint or dependency it joins the request's
transaction (so errors still roll everything back). In a plain script it runs in
a transaction of its own. In `async def` code, just use `await`.

---

## Where sessions come from

You never create or pass sessions for normal work. For every ORM call, the
session is chosen in this order:

1. `qs.using(session)` / `obj.save(session=...)` (explicit)
2. the current `atomic()` / `db.session()` block
3. the current HTTP request
4. otherwise: a short transaction just for this call

---

## Common errors

| Error | Meaning / fix |
|---|---|
| `RelationNotLoaded: Post.author is not loaded...` | Load the relation: `.prefetch("author")` or `await post.load("author")`. The message says which. |
| `FieldError: Post has no field 'x'. Did you mean ...?` | A typo in a lookup. |
| `SessionError: There is no active database session here` | `current_session()` outside a request or `atomic()`. Wrap the code in `async with atomic():`. |
| `Post.DoesNotExist` in a request | Returned as 404 automatically. Catch it where you want other behaviour. |
| HTTP 409 | A unique / foreign-key constraint failed (`IntegrityError`). |

## Performance

The query layer builds a normal SQLAlchemy statement, so the database work is
identical; the cost is Python overhead per query. Measured with
`benchmarks/orm_overhead.py` (SQLite, one session, a Windows laptop; numbers are noisy):

| Query | Plain SQLAlchemy | fastapi-mvt | Overhead |
|---|---|---|---|
| Primary-key lookup | ~550 µs | ~690 µs | about +0.15 ms |
| Filtered, ordered list of 20 rows | ~1.64 ms | ~1.73 ms | about +0.1 ms (+6%) |

Against a networked database, where a round trip usually costs 0.5–2 ms, this is
small. On a hot path, use `qs.statement` or `current_session()` directly.
