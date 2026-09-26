"""End-to-end tests of the migration workflow.

Every command runs in a subprocess inside a freshly generated project, exactly
as a developer would run it. Set TEST_DATABASE_URL to run against PostgreSQL.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from fastapi_mvt.scaffold import start_project

pytestmark = pytest.mark.slow


class Project:
    def __init__(self, root: Path, env: dict[str, str]) -> None:
        self.root = root
        self.env = env

    def run(self, *args: str, ok: "bool | None" = True, stdin: str = "") -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, "-m", "fastapi_mvt.cli", *args],
            cwd=self.root,
            env=self.env,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        output = result.stdout + result.stderr
        if ok is True and result.returncode != 0:
            raise AssertionError(f"`{' '.join(args)}` failed ({result.returncode}):\n{output}")
        if ok is False and result.returncode == 0:
            raise AssertionError(f"`{' '.join(args)}` should have failed:\n{output}")
        result.output = output  # type: ignore[attr-defined]
        return result

    def python(self, code: str) -> str:
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            cwd=self.root, env=self.env, capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode != 0:
            raise AssertionError(result.stdout + result.stderr)
        return result.stdout

    def write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")

    def makemigrations_interactive(self, answers: list[str], *extra: str) -> str:
        """Run makemigrations with scripted answers to its questions."""
        return self.python(
            f"""
            from fastapi_mvt.migrations import commands
            from fastapi_mvt.migrations.autogen import Prompter
            from fastapi_mvt.project import get_project
            answers = {answers!r}
            asked = []
            def reply(text):
                asked.append(text)
                return answers.pop(0)
            prompter = Prompter(interactive=True, confirm=lambda t: reply(t) == "y", ask=reply)
            result = commands.makemigrations(get_project(), prompter=prompter)
            print("ASKED:", asked)
            print("OPS:", result.plan.operations)
            print("PATH:", result.path)
            """
        )

    def sql(self, statement: str) -> str:
        return self.python(
            f"""
            import asyncio
            from sqlalchemy import text
            from fastapi_mvt.project import get_project, load_database
            db = load_database(get_project())
            async def main():
                async with db.engine.begin() as conn:
                    result = await conn.execute(text({statement!r}))
                    print(result.fetchall() if result.returns_rows else "done")
                await db.dispose()
            asyncio.run(main())
            """
        )


@pytest.fixture
def project(tmp_path: Path) -> Project:
    root = start_project("shop", tmp_path)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("TEST_DATABASE_URL", None)
    if os.environ.get("TEST_DATABASE_URL", "").startswith("postgres"):
        # A fresh database per test so projects never see each other's tables.
        base = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[0]
        name = f"mvt_{uuid.uuid4().hex[:10]}"
        _admin_sql(base + "/postgres", f'CREATE DATABASE "{name}"')
        env["DATABASE_URL"] = f"{base}/{name}"
        yield Project(root, env)
        _admin_sql(base + "/postgres", f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        return
    yield Project(root, env)


def _admin_sql(url: str, statement: str) -> None:
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from fastapi_mvt.db import to_async_url

    async def main() -> None:
        engine = create_async_engine(to_async_url(url), isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            await conn.execute(text(statement))
        await engine.dispose()

    asyncio.run(main())


BLOG_V1 = """
    from typing import Optional
    from sqlalchemy import String
    from fastapi_mvt.db import CASCADE, Mapped, TimestampedModel, fk, mapped_column, relationship

    class Post(TimestampedModel):
        title: Mapped[str] = mapped_column(String(200))
        body: Mapped[Optional[str]]
"""


def test_full_workflow(project: Project):
    p = project
    p.run("startapp", "blog", "--crud", "Post")
    out = p.run("makemigrations").output
    assert "0001_initial.py" in out and "Create table posts" in out and "Create table users" in out
    assert "Applying 0001_initial... OK" in p.run("migrate").output
    assert "No changes detected" in p.run("makemigrations").output
    assert "No migrations to apply" in p.run("migrate").output
    assert "[X] 0001_initial" in p.run("showmigrations").output
    assert "no issues" in p.run("check").output

    # Add a field -> Django-style auto name; `migrate` warns about model changes not yet migrated.
    models = p.root / "blog/models.py"
    models.write_text(
        models.read_text().replace("import CharField,", "import CharField, IntegerField,")
        + "    views = IntegerField(default=0)\n"
    )
    assert "Your models have changes" in p.run("migrate").output
    assert p.run("check", ok=False).returncode == 1
    assert p.run("makemigrations", "--check", ok=False).returncode == 1
    out = p.run("makemigrations").output
    assert "0002_posts_views.py" in out and "Add column views to posts" in out
    p.run("migrate")

    # Move backwards and forwards, Django style.
    out = p.run("migrate", "0001").output
    assert "Unapplying 0002_posts_views... OK" in out
    assert "[ ] 0002_posts_views" in p.run("showmigrations").output
    p.run("migrate")
    assert "Unapplying 0002_posts_views" in p.run("rollback").output
    p.run("migrate")
    assert "Unapplying 0001_initial" in p.run("migrate", "zero").output
    p.run("migrate")

    # Generated tests run against a migrated throwaway database, inside rolled-back transactions.
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=p.root, env=p.env, capture_output=True, text=True, encoding="utf-8",
    )
    assert tests.returncode == 0, tests.stdout + tests.stderr
    assert "3 passed" in tests.stdout


def test_broken_models_module_stops_everything(project: Project):
    p = project
    p.run("makemigrations")
    p.run("migrate")
    p.write("users/helpers.py", "")
    original = (p.root / "users/models.py").read_text()
    p.write("users/models.py", "import a_module_that_does_not_exist\n" + original)
    out = p.run("makemigrations", ok=False).output
    assert "Could not import models module 'users.models'" in out
    assert "a_module_that_does_not_exist" in out
    assert not (p.root / "migrations/versions/0002_auto.py").exists()
    assert len(list((p.root / "migrations/versions").glob("*.py"))) == 1


def test_destructive_changes_need_confirmation(project: Project):
    p = project
    p.run("startapp", "blog")
    p.write("blog/models.py", BLOG_V1)
    p.run("makemigrations")
    p.run("migrate")
    p.write("blog/models.py", "")  # the model was deleted
    out = p.run("makemigrations", "--no-input", ok=False).output
    assert "delete data" in out.lower() and "Drop table 'posts'" in out
    assert len(list((p.root / "migrations/versions").glob("*.py"))) == 1

    out = p.run("makemigrations", "--allow-destructive").output
    assert "Drop table posts" in out
    p.run("migrate")


def test_column_rename_is_detected_interactively(project: Project):
    p = project
    p.run("startapp", "blog")
    p.write("blog/models.py", BLOG_V1)
    p.run("makemigrations")
    p.run("migrate")
    p.sql("INSERT INTO posts (title, body, created_at, updated_at) VALUES ('t', 'keep me', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")

    p.write("blog/models.py", BLOG_V1.replace("body:", "content:"))
    out = p.makemigrations_interactive(["y"])
    assert "Was posts.body renamed to posts.content?" in out
    assert "Rename column posts.body to content" in out
    p.run("migrate")
    assert "keep me" in p.sql("SELECT content FROM posts")
    p.run("migrate", "0001")  # the rename reverses cleanly
    assert "keep me" in p.sql("SELECT body FROM posts")


def test_model_class_rename_keeps_the_data(project: Project):
    p = project
    p.run("startapp", "blog")
    p.write("blog/models.py", BLOG_V1)
    p.run("makemigrations")
    p.run("migrate")
    p.sql("INSERT INTO posts (title, created_at, updated_at) VALUES ('survivor', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")

    p.write("blog/models.py", BLOG_V1.replace("class Post", "class Article"))
    out = p.makemigrations_interactive(["y"])
    assert "Was table 'posts' renamed to 'articles'" in out
    p.run("migrate")
    assert "survivor" in p.sql("SELECT title FROM articles")


def test_not_null_column_on_a_table_with_rows(project: Project):
    p = project
    p.run("startapp", "blog")
    p.write("blog/models.py", BLOG_V1)
    p.run("makemigrations")
    p.run("migrate")
    p.sql("INSERT INTO posts (title, created_at, updated_at) VALUES ('old', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    p.write("blog/models.py", BLOG_V1 + "        status: Mapped[str] = mapped_column(String(20))\n")

    # Non-interactive: refuse to write a migration that would crash on `migrate`.
    out = p.run("makemigrations", "--no-input", ok=False).output
    assert "NOT NULL with no database default" in out and "Existing rows in posts need a value" in out

    # Interactive: ask for a one-off value, like Django.
    out = p.makemigrations_interactive(["'draft'"])
    assert "Enter a one-off value" in out
    p.run("migrate")
    assert "draft" in p.sql("SELECT status FROM posts")
    # The one-off default is not left behind in the schema.
    assert "No changes detected" in p.run("makemigrations").output


def test_makemigrations_ignores_the_state_of_the_dev_database(project: Project):
    """Like Django, models are compared with the migration history, not the dev database."""
    p = project
    p.run("makemigrations")                          # 0001, never applied to the dev database
    p.sql("CREATE TABLE scratch_notes (id INTEGER PRIMARY KEY)")   # manual drift in the dev database
    p.run("startapp", "blog")
    p.write("blog/models.py", BLOG_V1)
    out = p.run("makemigrations").output              # works without `migrate` first
    assert "0002_create_posts.py" in out and "scratch_notes" not in out
    p.run("migrate")
    assert "No changes detected" in p.run("makemigrations").output


def test_python_defaults_fill_existing_rows_without_asking(project: Project):
    p = project
    p.run("startapp", "blog")
    p.write("blog/models.py", BLOG_V1)
    p.run("makemigrations")
    p.run("migrate")
    p.sql("INSERT INTO posts (title, created_at, updated_at) VALUES ('old', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    models = p.root / "blog/models.py"
    models.write_text(
        models.read_text().replace("from fastapi_mvt.db import", "from fastapi_mvt.db import IntegerField,")
        + "    views = IntegerField(default=7)\n"
    )
    assert "Add column views to posts" in p.run("makemigrations", "--no-input").output
    p.run("migrate")
    assert "7" in p.sql("SELECT views FROM posts")
    assert "No changes detected" in p.run("makemigrations").output


def test_custom_management_commands(project: Project):
    p = project
    p.run("startapp", "blog")
    p.write("blog/models.py", BLOG_V1)
    p.write(
        "blog/commands.py",
        """
        from fastapi_mvt.commands import command
        from blog.models import Post

        @command()
        async def seed_posts(count: int = 2, prefix: str = "Post") -> None:
            \"\"\"Create some example posts.\"\"\"
            for i in range(count):
                await Post.objects.create(title=f"{prefix} {i}")
            print(f"created {await Post.objects.count()}")

        @command(name="hello")
        def say_hello(name: str) -> None:
            print(f"hello {name}")
        """,
    )
    p.run("makemigrations")
    p.run("migrate")
    assert "seed_posts" in p.run("--help").output
    assert "created 3" in p.run("seed_posts", "--count", "3").output
    assert "hello Ada" in p.run("hello", "Ada").output
    assert "Create some example posts" in p.run("seed_posts", "--help").output


def test_generated_code_passes_ruff_and_mypy(project: Project):
    p = project
    p.run("startapp", "catalog", "--crud", "Product")
    p.run("startapp", "blog")
    lint = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "E,F", "--ignore", "E501", "."],
        cwd=p.root, capture_output=True, text=True, encoding="utf-8",
    )
    assert lint.returncode == 0, lint.stdout + lint.stderr
    pytest.importorskip("mypy")
    probe = p.root / "mypy_probe.py"
    probe.write_text("import fastapi_mvt.db\n")
    visible = subprocess.run(
        [sys.executable, "-m", "mypy", str(probe)], cwd=p.root, capture_output=True, text=True, encoding="utf-8"
    )
    probe.unlink()
    if "import-not-found" in visible.stdout:
        pytest.skip(
            "mypy can't see fastapi_mvt (a PEP 660 editable install); "
            'reinstall with: pip install -e . --config-settings editable_mode=compat'
        )
    types = subprocess.run(
        [sys.executable, "-m", "mypy", "shop", "users", "catalog", "blog", "--ignore-missing-imports"],
        cwd=p.root, capture_output=True, text=True, encoding="utf-8",
    )
    assert types.returncode == 0, types.stdout + types.stderr


def test_async_tests_are_isolated_automatically(project: Project):
    """A test that forgets the `db` fixture must not leak rows into the next test."""
    p = project
    p.run("makemigrations")
    p.write(
        "tests/test_isolation.py",
        """
        from users.models import User

        async def test_1_writes_without_asking_for_db():
            await User.objects.create(email="leak@example.com", password="!")

        async def test_2_sees_a_clean_database():
            assert not await User.objects.filter(email="leak@example.com").exists()
        """,
    )
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_isolation.py"],
        cwd=p.root, env=p.env, capture_output=True, text=True, encoding="utf-8",
    )
    assert tests.returncode == 0, tests.stdout + tests.stderr


def test_empty_data_migration_and_sqlmigrate(project: Project):
    p = project
    p.run("makemigrations")
    out = p.run("makemigrations", "--empty", "--name", "backfill_names").output
    assert "0002_backfill_names.py" in out
    source = (p.root / "migrations/versions/0002_backfill_names.py").read_text()
    assert "Data migration example" in source
    p.run("migrate")
    sql = p.run("sqlmigrate", "0001", ok=None)  # SQLite batch mode may refuse; both outcomes are explained
    assert "CREATE TABLE" in sql.output or "batch mode" in sql.output


def test_conflicting_branches_are_merged(project: Project):
    p = project
    p.run("makemigrations")
    p.run("migrate")
    versions = p.root / "migrations/versions"
    for branch in ("a", "b"):
        (versions / f"0002_branch_{branch}.py").write_text(
            textwrap.dedent(
                f'''
                """branch {branch}"""
                revision = "0002_branch_{branch}"
                down_revision = "0001_initial"
                branch_labels = None
                depends_on = None
                def upgrade():
                    pass
                def downgrade():
                    pass
                '''
            )
        )
    out = p.run("migrate", ok=False).output
    assert "Conflicting migrations detected" in out and "makemigrations --merge" in out
    assert "merge" in p.run("makemigrations", "--merge").output
    p.run("migrate")
    assert "[ ]" not in p.run("showmigrations").output


def test_startapp_never_overwrites(project: Project):
    p = project
    p.run("startapp", "blog")
    (p.root / "blog/router.py").write_text("# my code\n")
    out = p.run("startapp", "blog", ok=False).output
    assert "conflicts with an existing" in out or "Refusing to overwrite" in out
    assert (p.root / "blog/router.py").read_text() == "# my code\n"


def test_adopting_an_existing_database_with_fake(project: Project):
    p = project
    p.run("makemigrations")
    p.run("migrate")
    p.sql("INSERT INTO users (email, password, is_active, is_superuser, token_version, created_at, updated_at) "
          "VALUES ('keep@example.com', '!', true, false, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    # Pretend the tables were created outside migrations: forget the migration history.
    p.run("migrate", "zero", "--fake")
    assert "[ ] 0001_initial" in p.run("showmigrations").output
    p.run("migrate", "--fake")
    assert "[X] 0001_initial" in p.run("showmigrations").output
    assert "keep@example.com" in p.sql("SELECT email FROM users")  # nothing was run against the tables
    assert "No changes detected" in p.run("makemigrations").output


def test_making_a_column_required_fills_existing_nulls(project: Project):
    p = project
    p.run("startapp", "blog")
    p.write("blog/models.py", BLOG_V1)
    p.run("makemigrations")
    p.run("migrate")
    p.sql("INSERT INTO posts (title, created_at, updated_at) VALUES ('no body', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    p.write("blog/models.py", BLOG_V1.replace("body: Mapped[Optional[str]]", "body: Mapped[str]"))

    out = p.run("makemigrations", "--no-input", ok=False).output
    assert "becomes NOT NULL, so existing NULL values need a replacement" in out
    out = p.makemigrations_interactive(["'(empty)'"])
    assert "Run SQL (data fix)" in out
    p.run("migrate")
    assert "(empty)" in p.sql("SELECT body FROM posts")
    assert "No changes detected" in p.run("makemigrations").output
