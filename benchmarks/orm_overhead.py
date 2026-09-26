"""How much does the Django-style layer cost compared with plain SQLAlchemy?

    python benchmarks/orm_overhead.py

Runs the same primary-key lookup and filtered list query three ways, against
SQLite in one session (so the numbers show the layer's overhead, not network):

* plain SQLAlchemy:  await session.execute(select(Post).where(...))
* QuerySet:          await Post.objects.using(session).get(id=...)
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import tempfile
import time
from pathlib import Path

from sqlalchemy import String, select
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi_mvt.db import Database, Mapped, Model, mapped_column  # noqa: E402


class BenchPost(Model):
    title: Mapped[str] = mapped_column(String(100))
    views: Mapped[int] = mapped_column(default=0)


ROWS = 1_000
ROUNDS = 5
OPS = 2_000


async def timed(label: str, operation) -> float:
    samples = []
    for _ in range(ROUNDS):
        start = time.perf_counter()
        for i in range(OPS):
            await operation(i)
        samples.append((time.perf_counter() - start) / OPS * 1_000_000)
    best = min(samples)
    print(f"  {label:<44} {best:8.1f} µs/op   (median {statistics.median(samples):.1f})")
    return best


async def main() -> None:
    db = Database(f"sqlite:///{Path(tempfile.mkdtemp(), 'bench.sqlite3').as_posix()}", poolclass=NullPool)
    await db.create_all()
    async with db.session() as session:
        session.add_all(BenchPost(title=f"post {i}", views=i) for i in range(ROWS))

    async with db.session() as session:
        print(f"Primary-key lookup ({OPS} ops, best of {ROUNDS}):")
        async def raw_get(i: int) -> None:
            result = await session.execute(select(BenchPost).where(BenchPost.id == i % ROWS + 1))
            result.scalars().one()

        raw = await timed("plain SQLAlchemy select().where(id == x)", raw_get)
        orm = await timed(
            "BenchPost.objects.using(s).get(id=x)",
            lambda i: BenchPost.objects.using(session).get(id=i % ROWS + 1),
        )
        print(f"  overhead: {orm - raw:+.1f} µs/op ({(orm / raw - 1) * 100:+.0f}%)\n")

        print(f"Filtered list of 20 rows ({OPS} ops, best of {ROUNDS}):")

        async def raw_list(i: int) -> None:
            result = await session.execute(
                select(BenchPost).where(BenchPost.views >= i % 900).order_by(BenchPost.views.desc()).limit(20)
            )
            result.scalars().all()

        raw = await timed("plain SQLAlchemy", raw_list)
        orm = await timed(
            'objects.filter(views__gte=x).order_by("-views")[:20]',
            lambda i: BenchPost.objects.using(session).filter(views__gte=i % 900).order_by("-views")[:20].all(),
        )
        print(f"  overhead: {orm - raw:+.1f} µs/op ({(orm / raw - 1) * 100:+.0f}%)")
    await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
