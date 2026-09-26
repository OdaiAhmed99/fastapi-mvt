"""Models shared by the in-process test modules."""

from __future__ import annotations

import enum
from typing import ClassVar, Optional

from sqlalchemy import Column, String, Table, UniqueConstraint
from sqlalchemy import ForeignKey as SAForeignKey

from fastapi_mvt.auth import AbstractUser
from fastapi_mvt.db import (
    CASCADE,
    PROTECT,
    SET_NULL,
    Base,
    BooleanField,
    CharField,
    DateField,
    DateTimeField,
    DecimalField,
    EnumField,
    ForeignKey,
    IntegerField,
    JSONField,
    Manager,
    Mapped,
    Model,
    QuerySet,
    TextField,
    TimestampedModel,
    fk,
    mapped_column,
    relationship,
)

post_tags = Table(
    "post_tags",
    Base.metadata,
    Column("post_id", SAForeignKey("posts.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", SAForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Author(TimestampedModel):
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[Optional[str]] = mapped_column(String(200), unique=True)
    posts: Mapped[list["Post"]] = relationship(back_populates="author", foreign_keys="Post.author_id")


class Tag(Model):
    name: Mapped[str] = mapped_column(String(30), unique=True)
    active: Mapped[bool] = mapped_column(default=True)


class Post(TimestampedModel):
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[Optional[str]]
    views: Mapped[int] = mapped_column(default=0)
    author_id: Mapped[int] = fk(Author, on_delete=CASCADE)
    editor_id: Mapped[Optional[int]] = fk("Author", on_delete=SET_NULL)
    author: Mapped[Author] = relationship(back_populates="posts", foreign_keys=[author_id])
    tags: Mapped[list[Tag]] = relationship(secondary=post_tags)
    comments: Mapped[list["Comment"]] = relationship(back_populates="post")


class Comment(Model):
    post_id: Mapped[int] = fk(Post, on_delete=CASCADE)
    text: Mapped[str] = mapped_column(String(500))
    post: Mapped[Post] = relationship(back_populates="comments")


class Membership(Model):
    __table_args__ = (UniqueConstraint("group", "member"),)
    group: Mapped[str] = mapped_column(String(50))
    member: Mapped[str] = mapped_column(String(50))


class Account(AbstractUser, TimestampedModel):
    full_name: Mapped[Optional[str]] = mapped_column(String(150))


class Level(str, enum.Enum):
    BEGINNER = "beginner"
    EXPERT = "expert"


class Course(Model):
    level: Mapped[Level] = mapped_column(server_default=Level.BEGINNER.value)


class ArticleQuerySet(QuerySet["Article"]):
    def published(self) -> "ArticleQuerySet":
        return self.filter(published=True)


class Article(Model):
    objects: ClassVar[Manager[ArticleQuerySet]] = Manager(ArticleQuerySet)
    title: Mapped[str] = mapped_column(String(100))
    published: Mapped[bool] = mapped_column(default=False)
    tag_id: Mapped[Optional[int]] = fk(Tag, on_delete=PROTECT)


class Genre(str, enum.Enum):
    FICTION = "fiction"
    SCIENCE = "science"


class Book(TimestampedModel):
    """Declared with the Django-style field helpers."""

    title = CharField(max_length=120)
    summary = TextField(null=True)
    pages = IntegerField(default=0)
    price = DecimalField(max_digits=8, decimal_places=2, null=True)
    in_print = BooleanField(default=True)
    genre = EnumField(Genre, default=Genre.FICTION)
    published_on = DateField(null=True)
    last_read = DateTimeField(null=True)
    metadata_ = JSONField(default=dict)
    author_id = ForeignKey(Author, on_delete=CASCADE)
    editor_id = ForeignKey(Author, on_delete=SET_NULL, null=True)
