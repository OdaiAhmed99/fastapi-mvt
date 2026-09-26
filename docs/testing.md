# Testing guide

```bash
pip install -e ".[test]"
pytest
```

The pytest plugin is active as soon as fastapi-mvt is installed. There's nothing
to configure.

## Fixtures

| Fixture | What you get |
|---|---|
| `client` | `httpx.AsyncClient` for your app (`await client.get("/items")`) |
| `db` | your `Database`, with the test wrapped in a transaction that is rolled back afterwards |

`client` uses `db`, so requests and your test code share one transaction and
see each other's writes, and all of it disappears after the test.

**Every `async def` test gets `db` automatically**, so a test that forgets to
ask for it still can't leak rows into the next test. To opt a test out (for
example one that manages its own connections), mark it `@pytest.mark.no_db`.

```python
from users.models import User

async def test_signup(client):
    response = await client.post("/auth/register", json={"email": "a@b.io", "password": "long-password"})
    assert response.status_code == 201
    assert await User.objects.filter(email="a@b.io").exists()
```

Tests are plain `async def` functions (`asyncio_mode = "auto"` is set in the
generated `pyproject.toml`).

## The test database

| Your DATABASE_URL | Test database |
|---|---|
| SQLite | a temporary file, deleted afterwards |
| PostgreSQL | `<name>_test`, created at the start and dropped at the end |
| anything else | set `TEST_DATABASE_URL` |

`TEST_DATABASE_URL` always wins. The schema is created **by running your
migrations**, so a broken migration fails the test run, not your deploy. If
models have changes that no migration covers, the run stops with
"run makemigrations first". A project without migrations falls back to `create_all()`.

## Authenticated requests

Create a user and mint a token directly instead of calling `/auth/login` in every test:

```python
import pytest
from myproject.auth import auth
from users.models import User

@pytest.fixture
async def headers(db):
    user = await User.objects.create(email="me@example.com", password="!")   # "!" = no usable password
    return {"Authorization": f"Bearer {auth.create_access_token(user)}"}

async def test_create_post(client, headers):
    response = await client.post("/posts", json={"title": "Hi"}, headers=headers)
    assert response.status_code == 201
```

## Tips

* Test business rules by calling your service functions directly; they're plain
  async functions.
* Run the same suite against PostgreSQL in CI by setting `TEST_DATABASE_URL`
  (see the repository's GitHub workflow for a service-container example).
