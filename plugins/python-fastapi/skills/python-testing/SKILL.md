---
name: python-testing
description: Test strategy and authoring in Python/FastAPI — unit tests of managers with fakes, integration tests of repositories against a real database, end-to-end tests of the API through httpx, fixtures, conftest, coverage, and what NOT to test. Use ALWAYS when writing, fixing or reviewing tests in a Python project: "write a test for this", "how do I test this endpoint", "this test is flaky", "I need to mock the database", pytest, pytest-asyncio, testcontainers, `TestClient`, `AsyncClient`, fixture, `conftest.py`, coverage. Trigger also when creating new code that has no test yet, and whenever someone proposes `mock.patch` — there is almost always a better option.
---

# Testing in Python/FastAPI

## The three layers and what each one buys

The classic pyramid gets it wrong by talking about quantity. What decides the kind
of test is **which risk it eliminates** — and each layer eliminates a risk the
others cannot catch:

| Layer | Eliminates the risk that | Cost | Healthy proportion |
|---|---|---|---|
| Unit | the business rule is wrong | ms, no I/O | most |
| Integration | the query/SQL/mapping is wrong | seconds, real DB | per repository |
| E2E | the wiring is wrong (route, DI, auth, serialization) | seconds, whole app | critical flows |

The expensive mistake is not having too few of one kind — it is using the wrong
kind. Testing a business rule through E2E costs 100x more and fails for reasons
unrelated to the rule. Testing ORM mapping with a mock tests nothing: the mock
returns whatever you told it to return.

## Unit: the manager with fakes

If the manager receives its dependencies as `Protocol`s in `__init__` (see
`fastapi-architecture`), the test needs no mock, no database and no network:

```python
class FakeOrderRepository:
    def __init__(self, orders: list[Order] | None = None) -> None:
        self._by_id = {o.id: o for o in orders or []}
        self.saved: list[Order] = []

    async def get(self, order_id: OrderId) -> Order | None:
        return self._by_id.get(order_id)

    async def save(self, order: Order) -> None:
        self.saved.append(order)


async def test_pay_does_not_charge_when_amount_mismatches() -> None:
    gateway = FakePaymentGateway()
    manager = OrderManager(orders=FakeOrderRepository([order]), payments=gateway)

    with pytest.raises(AmountMismatch):
        await manager.pay(OrderId("o1"), 999)

    assert gateway.calls == []
```

**Why a fake and not `Mock`:** the fake has behavior — it records what was saved and
returns what was stored. That enables assertions about **side effects**
(`gateway.calls == []`, "the customer was not charged"), which is exactly what
matters in this test and what `assert_not_called()` expresses far more fragilely.
The fake is also checked by the type checker: if the `Protocol` gains a method, mypy
flags the stale fake. `Mock` accepts anything — including a method that no longer
exists, while the test keeps "passing" and testing nothing.

**When `mock.patch` is justified:** `datetime.now`, `uuid4`, `random` — global
things you did not inject. Even then, injecting is better:

```python
class OrderManager:
    def __init__(self, orders, payments, now=lambda: datetime.now(UTC)) -> None:
        self._now = now      # test passes now=lambda: datetime(2026, 1, 1, tzinfo=UTC)
```

If you need `mock.patch` to test your own business rule, the problem is not the
test — it is the coupling in the code.

## What not to test

A test that only confirms the framework works is pure maintenance cost:

- Pydantic validation (`amount_cents: int = Field(gt=0)` needs no test)
- Getters/setters with no logic
- Route configuration (correct `response_model`) — E2E already covers it
- Third-party libraries

The criterion: **if the test cannot fail because of a bug of yours, it is not paying
for itself.**

## Base configuration

```toml
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"          # without this, every async test needs @pytest.mark.asyncio
testpaths = ["tests"]
addopts = "-q --strict-markers --strict-config"
markers = ["integration: needs a database", "e2e: boots the whole app"]
```

`--strict-markers` turns a typo'd marker into an error instead of silently skipping
the test — a test that does not run is the worst kind of failure, because it looks
like success.

Split by speed so the development loop stays fast:

```bash
pytest tests/unit                      # seconds, run on every save
pytest -m "integration or e2e"         # in CI, or before pushing
```

## Integration: repository against a real database

The real database is the whole point here. SQLite in place of Postgres saves
seconds and hides exactly the bugs you are trying to catch — `JSONB`, `ARRAY`,
`ON CONFLICT`, transaction behavior, collation.

```python
# tests/integration/conftest.py
import pytest
from testcontainers.postgres import PostgresContainer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture(scope="session")
def postgres_url() -> str:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.fixture(scope="session")
async def engine(postgres_url: str):
    engine = create_async_engine(postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine):
    """Each test runs in a transaction that is rolled back at the end."""
    async with engine.connect() as conn:
        transaction = await conn.begin()
        async with async_sessionmaker(bind=conn)() as s:
            yield s
        await transaction.rollback()
```

The **transaction-with-rollback per test** pattern is what makes the suite both fast
and isolated: the container starts once per session, and each test sees a clean
database without recreating the schema. Truncating tables between tests is orders of
magnitude slower and leaves residue when a test fails midway.

```python
@pytest.mark.integration
async def test_save_and_get_preserve_status(session) -> None:
    repo = SqlOrderRepository(session)
    order = Order(id=OrderId("o1"), customer_id=CustomerId("c1"), amount_cents=1000)
    order.mark_paid("rc_1")

    await repo.save(order)
    await session.flush()
    loaded = await repo.get(OrderId("o1"))

    assert loaded is not None
    assert loaded.status is OrderStatus.PAID
    assert loaded.receipt_id == "rc_1"
```

A repository test is always a round trip: save and read back. Only then is the
`entity ↔ row` mapping exercised in both directions — a conversion bug that only
exists on read goes unnoticed if the test only writes.

## E2E: the whole app through `AsyncClient`

E2E exists to catch what no lower layer can: the route is registered, DI composes,
auth runs, the status code is right, the JSON matches the published shape.

```python
# tests/e2e/conftest.py
import httpx
import pytest
from main import app
from api.deps import get_order_manager


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def override_manager():
    """Swap the real dependency for a controlled one, without touching the app."""
    def _override(manager):
        app.dependency_overrides[get_order_manager] = lambda: manager
    yield _override
    app.dependency_overrides.clear()
```

`ASGITransport` calls the app in-process — no port, no server, no waiting. And it is
genuinely async, unlike Starlette's `TestClient`, which runs its own loop internally
and deadlocks when the test also needs to `await`.

```python
@pytest.mark.e2e
async def test_pay_order_returns_200_with_paid_status(client, override_manager) -> None:
    override_manager(OrderManager(orders=FakeOrderRepository([order]), payments=FakePaymentGateway()))

    response = await client.post("/orders/o1/pay", json={"amount_cents": 1000})

    assert response.status_code == 200
    assert response.json()["status"] == "paid"


@pytest.mark.e2e
async def test_unknown_order_returns_404_with_code(client, override_manager) -> None:
    override_manager(OrderManager(orders=FakeOrderRepository(), payments=FakePaymentGateway()))

    response = await client.post("/orders/nope/pay", json={"amount_cents": 1000})

    assert response.status_code == 404
    assert response.json()["code"] == "order_not_found"
```

The second test is what justifies the layer: it verifies that the domain error became
a 404 through the exception handler. No unit test catches that — the manager only
knows how to raise `OrderNotFound`.

**Always clear `dependency_overrides` in the fixture.** A leaked override contaminates
subsequent tests, and the symptom is the worst possible one: an order-dependent
failure that does not reproduce in isolation.

## Names and structure

The test name is read in CI failure output, often by someone who did not write the
code. `test_pay_1` helps nobody at 2am.

```python
def test_<what>_<condition>_<expected_result>()

test_pay_with_mismatched_amount_does_not_charge_customer()
test_archive_of_already_archived_order_returns_409()
```

Arrange–Act–Assert separated by blank lines. One behavior per test — if the name
needs an "and", it is probably two tests.

## Coverage

Coverage detects **absence**; it does not measure quality. 100% coverage with weak
assertions proves nothing, but 0% on a business-rule module is an actionable fact.

```toml
[tool.coverage.report]
fail_under = 80
exclude_also = ["if TYPE_CHECKING:", "raise NotImplementedError", "@overload"]
```

If you enforce a threshold in CI, enforce it where it matters — `domain/` and
`managers/` deserve a high bar because they are pure and cheap to test. Demanding
90% on `infra/` pushes everyone to write adapter tests with mocks, which is exactly
the test that does not pay for itself.

## Checklist

- Manager test runs without a database, without the network and without `mock.patch`
- The fake implements the `Protocol` and records what it received
- Repository test uses real Postgres, round trip, with rollback per test
- E2E covers at least one happy path and one error path per route
- `dependency_overrides` cleared in a fixture
- The name states the condition and the expected result
- No test depends on execution order or on the clock
