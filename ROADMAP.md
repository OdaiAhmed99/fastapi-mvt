# Roadmap

Where fastapi-mvt stands after the 0.2 redesign, and what to do next, in order.
Written after a production review of the framework and the showcase.

## Where it stands (0.2.0)

Honest scorecard from the point of view of a Django developer moving to FastAPI:

| Area | Score | Notes |
|---|---|---|
| Feels familiar to Django developers | 9/10 | `objects.filter(a__b=...)`, `CharField`, `makemigrations`/`migrate`, `startapp`, `createsuperuser`, `shell`, custom commands, `get_or_create`, `Q`, `on_commit` |
| Migrations | 8.5/10 | Rename detection, data-loss confirmation, defaults for new fields, backwards `migrate <target>`, compares with migration history (scratch DB) |
| Safety and correctness | 8.5/10 | Request transactions, DB-enforced `on_delete`, explicit relation loading, per-test isolation; 87 tests on SQLite + PostgreSQL, oldest + latest dependencies |
| Learning curve | 7.5/10 | Async is new for Django developers; relationships still use `Mapped[...]` and `.prefetch()` |
| Documentation | 8/10 | Tutorial, guides, comparison page; no videos or community Q&A yet |
| Admin | 6/10 | SQLAdmin works but is far less customizable than Django admin |
| Auth | 6.5/10 | JWT, lockout, password reset; no permissions/groups, no social login |
| Ecosystem | 5/10 | FastAPI + SQLAlchemy packages instead of Django's ecosystem |
| Maturity and trust | 3/10 | 0.x, one author, no users, not on PyPI yet — the biggest weakness |

**Overall: 7/10 today, 8.5/10 reachable.** The blocker is trust and adoption, not features.

Among options for a Django developer moving to FastAPI: (1) if they really want
Django, Django + Django Ninja; (2) **fastapi-mvt** for the smoothest move to
FastAPI; (3) FastAPI + SQLModel (official template); (4) plain SQLAlchemy;
(5) Tortoise ORM.

## Phase 1 — Release (do first, ~1 day)

- [ ] Review and merge the `redesign-v0.2` pull requests (framework + showcase).
- [ ] Check that CI passes on GitHub (Python 3.10–3.13 × SQLite/PostgreSQL, package job).
- [ ] Enable GitHub Pages (Settings → Pages → Source: GitHub Actions) so `docs.yml` publishes the docs.
- [ ] Publish 0.2.0 to PyPI (create a GitHub release; `workflow.yml` publishes it).
- [ ] Update the showcase README install line once 0.2.0 is on PyPI.
- [ ] Decide on the name: "MVT" (Model-View-Template) no longer describes the library
      and invites the "Django clone" criticism. Candidates: `fastapi-orm-kit`,
      `fastapi-django-orm`, `djangoish`. Renaming is easiest before users arrive.

## Phase 2 — Trust and users (weeks 1–4)

- [ ] Reply to the original reviewer: show what changed because of their feedback.
- [ ] Post the tutorial + comparison page: FastAPI GitHub Discussions, r/FastAPI, r/django, dev.to.
- [ ] Get 3–5 real projects using it; collect every friction point as an issue.
- [ ] Add a "Used by" section and a short screencast of the first 5 minutes.
- [ ] Run CI weekly (scheduled) to catch breaking releases of SQLAlchemy/Alembic/FastAPI early.

## Phase 3 — Close the Django gaps (by user demand)

Highest value first:

- [ ] `annotate()` and `F()` expressions (`Post.objects.annotate(n=Count("comments"))`).
- [ ] Reverse relations without declaring both sides (Django's `related_name`).
- [ ] Permissions and groups in auth (`user.has_perm("events.add_event")`), with a `RequirePermission` dependency.
- [ ] `select_related` / `prefetch` with filtered relations (Django's `Prefetch` objects).
- [ ] `bulk_update()` and `update_or_create` on many rows.
- [ ] `ModelSchema` nested relations generated automatically (`depth=1`).
- [ ] Per-device logout (refresh-token table) as an option.

## Phase 4 — Reach and robustness (toward 1.0)

- [ ] Test and support MySQL/MariaDB (currently "experimental").
- [ ] Test Python 3.14; drop 3.10 when it reaches end of life.
- [ ] Multiple databases / read replicas (`using("replica")`).
- [ ] A migration squash command.
- [ ] Admin: richer defaults (search, filters, inlines) on top of SQLAdmin.
- [ ] Published benchmarks on PostgreSQL over the network (not just SQLite).
- [ ] 1.0: freeze the public API, publish a deprecation policy.

## Known limitations (documented, not bugs)

- ORM calls within one request share one session and run one at a time
  (`asyncio.gather` works, but isn't parallel); don't run `atomic()` blocks concurrently.
- Per-account lockout can be abused to lock accounts; rate-limit by IP at the proxy.
- `makemigrations` on PostgreSQL needs `CREATEDB` for its scratch database
  (it falls back to comparing with the live database and says so).
- Lookups like `title__icontains` are strings, so type checkers don't verify them
  (runtime errors include "did you mean ...").

## Verification baseline (0.2.0)

| Check | Result |
|---|---|
| Framework tests, latest deps, SQLite / PostgreSQL 16 | 87 / 87 passed |
| Framework tests, oldest deps (SQLAlchemy 2.0.36, pydantic 2.7, FastAPI 0.115, Alembic 1.14), SQLite / PostgreSQL | 84 passed, 3 skipped (admin) |
| Showcase, SQLite / PostgreSQL | 10 / 10 passed |
| ruff + mypy: framework, showcase, generated projects | clean |
| Clean install from the built wheel | generated project migrates, passes `check`, tests, ruff, mypy |
