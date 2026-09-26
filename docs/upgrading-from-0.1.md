# Upgrading from fastapi-mvt 0.1

0.2 is a rewrite. The concepts carry over, the API changes. The
[showcase repository](https://github.com/OdaiAhmed99/fastapi-mvt-showcase) was
ported this way; compare its `main` branch with `redesign-v0.2`.

## Quickest path

1. `pip install "fastapi-mvt[auth,server,test]>=0.2"`.
2. Generate a fresh project with the same package name in a scratch folder
   (`fastapi-mvt startproject <name>`), and copy its `pyproject.toml`,
   `manage.py`, `<name>/settings.py`, `db.py`, `auth.py`, `main.py` into your
   project. Keep your `.env`, but make sure it has a 32+ character `SECRET_KEY`.
3. Rename each app's `urls.py`/`views.py` into a `router.py` and include it in `main.py`.
4. Convert models and queries (below).
5. Adopt your existing database (below).

## Models

```python
# 0.1
from sqlalchemy import Column, Integer, String, ForeignKey
class Post(Base):
    __tablename__ = "posts"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    author_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))

# 0.2
from fastapi_mvt.db import CASCADE, Mapped, TimestampedModel, fk, mapped_column
class Post(TimestampedModel):
    title: Mapped[str] = mapped_column(String(200))
    author_id: Mapped[int] = fk("User", on_delete=CASCADE)
```

For the user model, use `class User(AbstractUser, TimestampedModel)`. 0.1's
`hashed_password` column is now `password`: add a rename when you run
`makemigrations` (answer *yes* to "Was users.hashed_password renamed to
users.password?"). Old bcrypt hashes keep working with `pip install bcrypt`.

## Queries and sessions

| 0.1 | 0.2 |
|---|---|
| `db: Session = Depends(get_db)` in every endpoint | nothing (remove it) |
| `db.query(Post).filter(Post.id == 1).first()` | `await Post.objects.get_or_none(id=1)` |
| `get_object_or_404(Post, db, id=1)` | `await Post.objects.get(id=1)` |
| `paginate(query, page, size)` | `await qs.paginate(params)` with `Page[Schema]` |
| `db.add(x); db.commit(); db.refresh(x)` | `await x.save()` (commit happens at the end of the request) |
| `def endpoint(...)` with sync session | `async def endpoint(...)` |
| `@receiver(post_save, sender=X)` | call your code, and use `on_commit(...)` for side effects |
| `configure_auth(AuthConfig(...))` | `auth = Auth(User, secret_key=...)` |
| `Depends(get_current_user)` | `user: CurrentUser` |
| `from fastapi_mvt.utils.websocket import ConnectionManager` | `from fastapi_mvt.websockets import ConnectionManager` |

## Adopting the existing database

0.1 created some tables with `create_all()`, outside migrations. Let 0.2 take over:

```bash
DATABASE_URL=sqlite:///./empty.sqlite3 python manage.py makemigrations   # 0001_initial from the models
python manage.py migrate --fake                                           # real DB: mark as applied
python manage.py makemigrations                                           # any real differences
```

Review the last migration carefully; it is where the old schema and the new
models differ.
