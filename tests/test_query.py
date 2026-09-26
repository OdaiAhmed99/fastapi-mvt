from __future__ import annotations

import pytest
from sqlalchemy import func, select

from fastapi_mvt.db import FieldError, ObjectDoesNotExist, Q, atomic, current_session
from tests.models import Author, Comment, Membership, Post, Tag


async def seed():
    ada = await Author.objects.create(name="Ada", email="ada@example.com")
    bob = await Author.objects.create(name="Bob", email=None)
    python = await Tag.objects.create(name="python")
    sql = await Tag.objects.create(name="sql", active=False)
    posts = [
        Post(title="Hello world", author=ada, views=10, tags=[python]),
        Post(title="SQL tips", author=ada, views=3, tags=[sql, python]),
        Post(title="Draft", author=bob, views=0, body="wip"),
    ]
    await Post.objects.bulk_create(posts)
    return ada, bob, python, sql, posts


async def titles(qs):
    return sorted(await qs.values_list("title", flat=True))


async def test_table_names_and_managers(db):
    assert Author.__tablename__ == "authors"
    assert Membership.__tablename__ == "memberships"
    with pytest.raises(AttributeError, match="Manager isn't accessible"):
        Author(name="x").objects  # noqa: B018


async def test_create_fills_ids_and_defaults(db):
    author = await Author.objects.create(name="Ada")
    assert author.id is not None and author.pk == author.id
    assert author.created_at is not None and author.updated_at is not None
    post = await Post.objects.create(title="t", author_id=author.id, views=None)  # None -> column default
    assert post.views == 0


async def test_filter_lookups(db):
    await seed()
    assert await titles(Post.objects.filter(title__icontains="HELLO")) == ["Hello world"]
    assert await titles(Post.objects.filter(title__startswith="SQL")) == ["SQL tips"]
    assert await titles(Post.objects.filter(views__gte=3, views__lt=10)) == ["SQL tips"]
    assert await titles(Post.objects.filter(views__in=[0, 10])) == ["Draft", "Hello world"]
    assert await titles(Post.objects.filter(body__isnull=False)) == ["Draft"]
    assert await titles(Post.objects.filter(body=None)) == ["Hello world", "SQL tips"]
    assert await titles(Post.objects.filter(views__range=(1, 5))) == ["SQL tips"]
    assert await titles(Post.objects.filter(title__contains="%")) == []  # wildcards are escaped


async def test_q_objects_and_expressions(db):
    await seed()
    qs = Post.objects.filter(Q(views__gt=5) | Q(title="Draft"))
    assert await titles(qs) == ["Draft", "Hello world"]
    assert await titles(Post.objects.filter(~Q(title="Draft"))) == ["Hello world", "SQL tips"]
    assert await titles(Post.objects.filter(Post.views > 5)) == ["Hello world"]


async def test_exclude_keeps_nulls_like_django(db):
    await seed()
    names = await Author.objects.exclude(email="ada@example.com").values_list("name", flat=True)
    assert names == ["Bob"]  # Bob's email is NULL and must not vanish


async def test_relation_lookups(db):
    ada, bob, python, sql, _ = await seed()
    assert await titles(Post.objects.filter(author__name="Bob")) == ["Draft"]
    assert await titles(Post.objects.filter(author=ada)) == ["Hello world", "SQL tips"]
    assert await titles(Post.objects.filter(tags__name="sql")) == ["SQL tips"]
    # One tag must satisfy both conditions (Django semantics for multi-valued relations).
    assert await titles(Post.objects.filter(tags__name="sql", tags__active=True)) == []
    assert await titles(Post.objects.filter(tags=python)) == ["Hello world", "SQL tips"]
    assert await titles(Post.objects.filter(tags__isnull=True)) == ["Draft"]
    assert sorted(await Author.objects.filter(posts__views__gte=10).values_list("name", flat=True)) == ["Ada"]


async def test_field_errors_suggest_fixes(db):
    with pytest.raises(FieldError, match="Did you mean 'title'"):
        Post.objects.filter(titel="x")
    with pytest.raises(FieldError, match="Did you mean 'icontains'"):
        Post.objects.filter(title__icontian="x")
    with pytest.raises(FieldError, match="not a relation"):
        Post.objects.prefetch("title")
    with pytest.raises(TypeError, match="plain bool"):
        Post.objects.filter(True)


async def test_get_errors(db):
    await seed()
    with pytest.raises(Post.DoesNotExist):
        await Post.objects.get(id=-1)
    with pytest.raises(ObjectDoesNotExist):
        await Post.objects.get(id=-1)
    with pytest.raises(Post.MultipleObjectsReturned):
        await Post.objects.get(author__name="Ada")
    assert await Post.objects.get_or_none(id=-1) is None
    # Each model has its own exception class.
    assert not issubclass(Post.DoesNotExist, Author.DoesNotExist)


async def test_ordering_slicing_first_last(db):
    await seed()
    by_views = await Post.objects.order_by("-views").values_list("views", flat=True)
    assert by_views == [10, 3, 0]
    assert [p.views for p in await Post.objects.order_by("views")[1:3]] == [3, 10]
    assert (await Post.objects.order_by("views").first()).views == 0
    assert (await Post.objects.order_by("views").last()).views == 10
    assert (await Post.objects.filter(views=-5).first()) is None
    with pytest.raises(TypeError):
        Post.objects[0]  # type: ignore[index]


async def test_count_exists_values_aggregate(db):
    await seed()
    assert await Post.objects.count() == 3
    assert await Post.objects.order_by("id")[:2].count() == 2
    assert await Post.objects.filter(views__gt=100).exists() is False
    assert await Post.objects.exists() is True
    assert await Comment.objects.exists() is False  # regression: an unfiltered exists() had no FROM
    assert await Comment.objects.aggregate(n=func.count()) == {"n": 0}
    rows = await Post.objects.filter(title="Draft").values("title", "views")
    assert rows == [{"title": "Draft", "views": 0}]
    stats = await Post.objects.aggregate(total=func.sum(Post.views), n=func.count(Post.id))
    assert stats == {"total": 13, "n": 3}
    by_id = await Post.objects.in_bulk([p.id for p in await Post.objects.all()])
    assert len(by_id) == 3


async def test_prefetch_and_explicit_loading(db):
    await seed()
    posts = await Post.objects.prefetch("author", "tags").order_by("id")
    assert [p.author.name for p in posts] == ["Ada", "Ada", "Bob"]
    assert {t.name for t in posts[1].tags} == {"sql", "python"}
    joined = await Post.objects.select_related("author").filter(title="Draft").get()
    assert joined.author.name == "Bob"

    post = await Post.objects.get(title="Draft")
    with pytest.raises(Exception, match="author"):
        post.author  # noqa: B018 - not loaded: must fail loudly, never do hidden I/O
    await post.load("author")
    assert post.author.name == "Bob"

    comment = await Comment.objects.create(post_id=post.id, text="hi")
    nested = await Comment.objects.prefetch("post__author").get(id=comment.id)
    assert nested.post.author.name == "Bob"


async def test_bulk_update_and_delete(db):
    await seed()
    assert await Post.objects.filter(author__name="Ada").update(views=Post.views + 1) == 2
    assert sorted(await Post.objects.values_list("views", flat=True)) == [0, 4, 11]
    assert await Post.objects.filter(views=0).delete() == 1
    assert await Post.objects.count() == 2
    with pytest.raises(FieldError):
        await Post.objects.update(author=None)
    with pytest.raises(TypeError, match="sliced"):
        await Post.objects[:1].delete()


async def test_instance_save_update_delete_refresh(db):
    author = Author(name="Ada")
    await author.save()
    assert author.id
    await author.update(name="Ada L.")
    assert (await Author.objects.get(id=author.id)).name == "Ada L."

    await Author.objects.filter(id=author.id).update(name="Changed elsewhere")
    await author.refresh()
    assert author.name == "Changed elsewhere"

    with pytest.raises(AttributeError, match="no field"):
        await author.update(nme="x")
    await author.delete()
    assert not await Author.objects.filter(id=author.id).exists()


async def test_delete_cascades_in_the_database(db):
    ada, bob, *_ = await seed()
    post = await Post.objects.get(title="Draft")
    post.editor_id = ada.id
    await post.save()
    await Comment.objects.create(post_id=post.id, text="nice")

    await ada.delete()
    # CASCADE: Ada's posts are gone; SET NULL: Bob's post keeps existing without editor.
    assert await titles(Post.objects.all()) == ["Draft"]
    draft = await Post.objects.get(title="Draft")
    assert draft.editor_id is None
    await bob.delete()
    assert await Comment.objects.count() == 0


async def test_get_or_create_and_update_or_create(db):
    tag, created = await Tag.objects.get_or_create(name="py", defaults={"active": False})
    assert created and tag.active is False
    again, created = await Tag.objects.get_or_create(name="py")
    assert not created and again.id == tag.id

    obj, created = await Tag.objects.update_or_create(name="py", defaults={"active": True})
    assert not created and obj.active is True
    obj, created = await Tag.objects.update_or_create(name="new", defaults={"active": False})
    assert created and obj.active is False


async def test_get_or_create_survives_a_concurrent_insert(db, monkeypatch):
    """Simulate another request inserting between our SELECT and INSERT."""
    from fastapi_mvt.db.query import QuerySet

    await Membership.objects.create(group="g", member="m")
    real = QuerySet.get_or_none
    calls = {"n": 0}

    async def racy(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # pretend the row did not exist yet
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "get_or_none", racy)
    obj, created = await Membership.objects.get_or_create(group="g", member="m")
    assert not created and obj.member == "m"


async def test_pagination(db):
    author = await Author.objects.create(name="A")
    await Post.objects.bulk_create([Post(title=f"p{i}", author=author) for i in range(7)])
    page = await Post.objects.order_by("id").paginate(page=2, size=3)
    assert (page.total, page.pages, page.page, page.size) == (7, 3, 2, 3)
    assert [p.title for p in page.items] == ["p3", "p4", "p5"]
    assert page.has_next and page.has_previous
    empty = await Post.objects.filter(title="nope").paginate()
    assert empty.total == 0 and empty.pages == 0 and not empty.has_next


async def test_statement_is_plain_sqlalchemy(db):
    await seed()
    qs = Post.objects.filter(author__name="Ada").order_by("-views")
    async with db.session() as session:
        rows = (await session.execute(qs.statement)).scalars().all()
    assert [p.views for p in rows] == [10, 3]
    assert "ORDER BY posts.views DESC" in qs.sql()
    assert "x''y" in Post.objects.filter(title="x'y").sql()


async def test_async_iteration_and_await(db):
    await seed()
    assert len(await Post.objects.filter(views__gt=0)) == 2
    seen = [p.title async for p in Post.objects.order_by("title")]
    assert seen == ["Draft", "Hello world", "SQL tips"]


async def test_using_an_explicit_session(db):
    async with db.session() as session:
        await Author.objects.using(session).create(name="Explicit")
        assert current_session() is session
        rows = (await session.execute(select(Author.name))).scalars().all()
        assert "Explicit" in rows


async def test_datetimes_are_always_utc_aware(db):
    from datetime import datetime, timedelta, timezone

    author = await Author.objects.create(name="tz")
    reloaded = await Author.objects.get(id=author.id)
    assert reloaded.created_at.tzinfo is not None
    assert reloaded.created_at.utcoffset() == timedelta(0)

    plus_two = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    await Author.objects.filter(id=author.id).update(created_at=plus_two)
    stored = (await Author.objects.get(id=author.id)).created_at
    assert stored == plus_two and stored.hour == 10  # same instant, normalised to UTC
    assert await Author.objects.filter(created_at__lt=datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)).exists()


async def test_enums_are_stored_as_their_values(db):
    from sqlalchemy import text

    from tests.models import Course, Level

    course = await Course.objects.create(level=Level.EXPERT)
    assert (await Course.objects.get(id=course.id)).level is Level.EXPERT
    assert await Course.objects.filter(level=Level.EXPERT).count() == 1
    async with db.session() as session:
        raw = (await session.execute(text("SELECT level FROM courses"))).scalar()
    assert raw == "expert"


async def test_custom_queryset_subquery_only_and_protect(db):
    from sqlalchemy.exc import IntegrityError

    from tests.models import Article

    tag = await Tag.objects.create(name="news")
    await Article.objects.bulk_create(
        [Article(title="a", published=True, tag_id=tag.id), Article(title="b"), Article(title="c", published=True)]
    )
    assert sorted(await Article.objects.published().values_list("title", flat=True)) == ["a", "c"]
    assert await Article.objects.published().filter(title="a").count() == 1

    tagged = Article.objects.filter(tag_id__isnull=False)
    assert await Article.objects.filter(id__in=tagged).values_list("title", flat=True) == ["a"]

    light = await Article.objects.only("id", "title").order_by("id").first()
    assert light.title == "a"

    with pytest.raises(IntegrityError):  # PROTECT: the database refuses to delete a referenced tag
        async with atomic():
            await tag.delete()
    assert await Tag.objects.filter(id=tag.id).exists()


async def test_django_style_fields(db):
    from decimal import Decimal

    from fastapi_mvt.db import model_schema
    from tests.models import Book, Genre

    table = Book.__table__
    assert not table.c.title.nullable and table.c.title.type.length == 120
    assert table.c.summary.nullable and table.c.editor_id.nullable and not table.c.author_id.nullable
    assert table.c.genre.server_default is not None  # addable to tables with rows

    author = await Author.objects.create(name="Ursula")
    book = await Book.objects.create(title="The Dispossessed", author_id=author.id, price=Decimal("9.99"))
    loaded = await Book.objects.get(id=book.id)
    assert loaded.genre is Genre.FICTION and loaded.pages == 0 and loaded.in_print is True
    assert loaded.price == Decimal("9.99") and loaded.metadata_ == {}
    assert await Book.objects.filter(genre=Genre.FICTION, title__icontains="dispossessed").count() == 1

    create = model_schema(Book, "create")
    assert create.model_fields["title"].is_required()
    assert not create.model_fields["pages"].is_required()

    with pytest.raises(ValueError, match="null=True"):
        from fastapi_mvt.db import SET_NULL, ForeignKey

        ForeignKey(Author, on_delete=SET_NULL)


async def test_unloaded_relations_explain_the_fix(db):
    from fastapi_mvt.db import RelationNotLoaded
    from fastapi_mvt.db.models import _EXPLICIT

    assert _EXPLICIT == "explicit", "the friendly loader failed to register with this SQLAlchemy version"
    author = await Author.objects.create(name="A")
    await Post.objects.create(title="t", author_id=author.id)
    post = await Post.objects.get(title="t")
    with pytest.raises(RelationNotLoaded, match=r'\.prefetch\("author"\)'):
        post.author  # noqa: B018


async def test_updated_at_changes_on_save_and_bulk_update(db):
    import asyncio

    author = await Author.objects.create(name="A")
    created = author.updated_at
    await asyncio.sleep(0.01)
    await author.update(name="B")
    assert author.updated_at > created
    after_save = author.updated_at
    await asyncio.sleep(0.01)
    await Author.objects.filter(id=author.id).update(name="C")
    assert (await Author.objects.get(id=author.id)).updated_at > after_save
