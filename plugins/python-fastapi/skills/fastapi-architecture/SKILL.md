---
name: fastapi-architecture
description: Layered architecture for Python/FastAPI services using the manager pattern — where each rule belongs, which layer may import which, how to build a new module, and how to keep the code clean. Use ALWAYS when creating, reviewing or refactoring code in a FastAPI service: new endpoint, new domain module, "where do I put this business rule", repository, manager, service, Pydantic schema vs domain entity, dependency injection, folder structure, or when a route file starts growing. Trigger even when the user never says "architecture", "clean arch" or "manager" — questions like "how should I organize this", "this endpoint is getting too big", or "where does this validation go" are the primary use case.
---

# FastAPI service architecture — manager pattern

## The real problem

A FastAPI service rots in a predictable way: the route starts at 5 lines, gains a
validation, then a query, then a business `if`, then an HTTP call to another
service. Six months later `router.py` is 800 lines, nothing can be tested without
booting the whole app, and swapping the database means touching 40 route files.

The manager pattern solves this with a single idea: **business rules must not know
that HTTP exists, and must not know that a database exists.** Everything else
follows from that.

## The layers

```
  HTTP  ─────────────────────────────────────────────────────┐
        api/           routes, status codes, serialization   │  knows FastAPI
        ──────────────────────────────────────────────────── │
        managers/      use cases, orchestration              │  pure Python
        ──────────────────────────────────────────────────── │
        domain/        entities, ports (Protocol), errors    │  imports nothing
        ──────────────────────────────────────────────────── │
        repositories/  implements the ports, talks to the DB │  knows SQLAlchemy
        infra/         engine, http client, cache, settings  │  knows the world
  I/O   ─────────────────────────────────────────────────────┘
```

## The rule that decides everything

**Dependencies point inward.** `infra` knows `domain`; `domain` knows nobody. In
practice this becomes a table of allowed imports — and that table is verifiable,
not philosophy:

| Layer | May import | Must never import |
|---|---|---|
| `api/` | `managers`, `domain`, schemas | `repositories`, `infra` directly |
| `managers/` | `domain` | `fastapi`, `sqlalchemy`, `httpx` |
| `domain/` | stdlib, `pydantic` (value objects only) | any outer layer |
| `repositories/` | `domain`, `infra` | `managers`, `api` |

If `managers/` imports `fastapi`, the rule has leaked. That is the most reliable
symptom that the architecture broke, and it can be enforced in CI with
`import-linter`.

The practical reason for banning `fastapi` in the manager is not purity: it is that
the day this same rule has to run in a queue consumer, a cron job or a migration
script, it runs unchanged. If it raises `HTTPException`, it does not.

## What a manager is

A manager is **one class per domain concept**, holding the use cases for that
concept. It is not a bag of utility functions, and it is not a 1:1 wrapper around
the repository — if every manager method just forwards to the repo, the layer is
not paying for itself and the rule probably ended up in the route.

```python
# managers/order_manager.py
from domain.order import Order, OrderStatus
from domain.errors import OrderNotFound, OrderAlreadyPaid
from domain.ports import OrderRepository, PaymentGateway


class OrderManager:
    def __init__(self, orders: OrderRepository, payments: PaymentGateway) -> None:
        self._orders = orders
        self._payments = payments

    async def pay(self, order_id: str, amount_cents: int) -> Order:
        order = await self._orders.get(order_id)
        if order is None:
            raise OrderNotFound(order_id)
        if order.status is OrderStatus.PAID:
            raise OrderAlreadyPaid(order_id)

        order.assert_amount_matches(amount_cents)   # invariant lives in the entity
        receipt = await self._payments.charge(order.customer_id, amount_cents)
        order.mark_paid(receipt.id)

        await self._orders.save(order)
        return order
```

Three things to notice:

1. **Dependencies arrive through `__init__`**, typed by *ports* (`Protocol`), not by
   concrete implementations. That is what makes the manager testable without a
   database and without the network.
2. **No framework type appears** — no `Request`, no `Session`, no `HTTPException`.
   The error raised is a domain error.
3. **The invariant lives in the entity** (`assert_amount_matches`, `mark_paid`). The
   manager orchestrates; the entity protects itself. A manager that only does
   `setattr` on a dataclass produces an anemic model, and the same rule ends up
   duplicated in three different places.

## Ports with `Protocol`, not ABC

```python
# domain/ports.py
from typing import Protocol
from domain.order import Order


class OrderRepository(Protocol):
    async def get(self, order_id: str) -> Order | None: ...
    async def save(self, order: Order) -> None: ...
```

`Protocol` is structural typing: the concrete repository inherits nothing, so
`domain` does not need to be imported by whoever implements it. With `ABC` you
create inheritance coupling and invert the dependency arrow exactly where it matters
most. Bonus: a test fake becomes a 6-line class.

## Where to put the rule — decision guide

When in doubt, answer in this order:

- **Is it an invariant of a single object?** (a value cannot be negative, a status
  only moves from A to B) → entity in `domain/`.
- **Does it coordinate several objects or several I/O calls?** (charge payment, then
  save, then publish an event) → manager.
- **Is it input/output shape?** (required field, valid email, formatting) → Pydantic
  schema in `api/`.
- **Is it a detail of how data is stored?** (join, index, upsert) → repository.

*Format* validation in Pydantic, *rule* validation in the domain. `email: EmailStr`
is Pydantic; "a blocked customer cannot purchase" never is.

## The route stays thin

```python
# api/routes/orders.py
from fastapi import APIRouter, Depends, status
from api.schemas.order import PayOrderIn, OrderOut
from api.deps import get_order_manager
from managers.order_manager import OrderManager

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("/{order_id}/pay", response_model=OrderOut, status_code=status.HTTP_200_OK)
async def pay_order(
    order_id: str,
    payload: PayOrderIn,
    manager: OrderManager = Depends(get_order_manager),
) -> OrderOut:
    order = await manager.pay(order_id, payload.amount_cents)
    return OrderOut.model_validate(order)
```

The route does exactly four things: receive, convert, delegate, return. If there is
a business `if` in there, it is in the wrong place. And there is no `try/except`
translating errors — that is the global exception handler's job, otherwise the same
`except` gets repeated in every route.

## Domain errors become HTTP in one place

```python
# api/errors.py
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from domain.errors import DomainError, NotFoundError, ConflictError

STATUS_BY_ERROR = {NotFoundError: 404, ConflictError: 409}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def handle_domain_error(_: Request, exc: DomainError) -> JSONResponse:
        status_code = next(
            (s for t, s in STATUS_BY_ERROR.items() if isinstance(exc, t)), 400
        )
        return JSONResponse(
            status_code=status_code,
            content={"detail": str(exc), "code": exc.code},
        )
```

One error hierarchy in `domain/errors.py`, one mapping to HTTP in `api/`. The domain
never picks a status code.

## Dependency injection — the seam lives in `api/deps.py`

```python
# api/deps.py
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from infra.db import get_session
from infra.payments import StripeGateway
from repositories.order_repository import SqlOrderRepository
from managers.order_manager import OrderManager


def get_order_manager(session: AsyncSession = Depends(get_session)) -> OrderManager:
    return OrderManager(orders=SqlOrderRepository(session), payments=StripeGateway())
```

This is the **only** file that knows every layer at once — it is the composition
root. The manager does not instantiate the repository on its own; if it did, the
test would have no way to swap the implementation.

## Folder structure

```
src/
  api/
    deps.py
    errors.py
    routes/orders.py
    schemas/order.py
  managers/order_manager.py
  domain/
    order.py           # entity + invariants
    ports.py
    errors.py
  repositories/order_repository.py
  infra/
    db.py
    settings.py
    http.py
  main.py
tests/
  unit/                # manager with fakes, pure entity
  integration/         # repository against a real database
  e2e/                 # whole app through httpx
```

In large services (more than ~6 domains), group by domain first
(`src/orders/{api,managers,domain,repositories}`) — a whole feature then fits in one
directory and coupling between domains becomes visible in the imports. The
dependency table still applies unchanged.

To generate a new module in this shape:
`python scripts/new_module.py <domain_name> --root src`

## Checklist before opening the PR

- No `fastapi`/`sqlalchemy` import inside `managers/` or `domain/`
- Route with no business `if` and no error-translating `try/except`
- Manager receives dependencies in `__init__`, typed by `Protocol`
- Entity protects its own invariants (not just a bag of fields)
- New error inherits from `DomainError` and has a mapped status
- Manager test runs without a database and without the network

## References

- `references/clean-code.md` — naming, function size, typing, settings, structured
  logging. Read it when writing or reviewing day-to-day code.
- `references/complete-module-example.md` — a whole module (entity, port, manager,
  repository, route, tests) to copy as a template. Read it when creating a module
  from scratch or when the shape of some layer is unclear.
- Concurrency, event loop and loop blocking have their own skill: `fastapi-async`.
