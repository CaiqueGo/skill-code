# Complete module — a vertical slice of `orders`

Every layer of one domain, in the order it should be written. Use it as a template
for a new module. Writing inside-out (domain first, route last) prevents the shape
of the incoming JSON from contaminating the business model.

## 1. `domain/errors.py`

```python
class DomainError(Exception):
    code: str = "domain_error"


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    code = "conflict"


class ValidationError(DomainError):
    code = "validation_error"


class OrderNotFound(NotFoundError):
    code = "order_not_found"

    def __init__(self, order_id: str) -> None:
        super().__init__(f"order {order_id} not found")
        self.order_id = order_id


class OrderAlreadyPaid(ConflictError):
    code = "order_already_paid"

    def __init__(self, order_id: str) -> None:
        super().__init__(f"order {order_id} is already paid")
        self.order_id = order_id


class AmountMismatch(ValidationError):
    code = "amount_mismatch"

    def __init__(self, expected_cents: int, got_cents: int) -> None:
        super().__init__(f"expected {expected_cents}, got {got_cents}")
        self.expected_cents = expected_cents
        self.got_cents = got_cents
```

## 2. `domain/order.py` — the entity

```python
from dataclasses import dataclass
from datetime import datetime, UTC
from enum import Enum
from typing import NewType

from domain.errors import AmountMismatch, OrderAlreadyPaid

OrderId = NewType("OrderId", str)
CustomerId = NewType("CustomerId", str)


class OrderStatus(Enum):
    PENDING = "pending"
    PAID = "paid"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class Order:
    id: OrderId
    customer_id: CustomerId
    amount_cents: int
    status: OrderStatus = OrderStatus.PENDING
    receipt_id: str | None = None
    paid_at: datetime | None = None

    def assert_amount_matches(self, amount_cents: int) -> None:
        if amount_cents != self.amount_cents:
            raise AmountMismatch(self.amount_cents, amount_cents)

    def mark_paid(self, receipt_id: str) -> None:
        if self.status is OrderStatus.PAID:
            raise OrderAlreadyPaid(self.id)
        self.status = OrderStatus.PAID
        self.receipt_id = receipt_id
        self.paid_at = datetime.now(UTC)
```

The entity is the only place that knows how to transition status. If a second
payment path appears one day, it reuses `mark_paid` and the rule does not duplicate.

## 3. `domain/ports.py` — what the manager needs from the world

```python
from dataclasses import dataclass
from typing import Protocol

from domain.order import CustomerId, Order, OrderId


@dataclass(frozen=True, slots=True)
class Receipt:
    id: str
    amount_cents: int


class OrderRepository(Protocol):
    async def get(self, order_id: OrderId) -> Order | None: ...
    async def save(self, order: Order) -> None: ...


class PaymentGateway(Protocol):
    async def charge(self, customer_id: CustomerId, amount_cents: int) -> Receipt: ...
```

The port describes what the domain needs, not the vendor's API. `charge` takes a
`CustomerId`, not a Stripe SDK object — otherwise the SDK has leaked inward.

## 4. `managers/order_manager.py`

```python
from domain.errors import OrderNotFound
from domain.order import Order, OrderId
from domain.ports import OrderRepository, PaymentGateway


class OrderManager:
    def __init__(self, orders: OrderRepository, payments: PaymentGateway) -> None:
        self._orders = orders
        self._payments = payments

    async def get(self, order_id: OrderId) -> Order:
        order = await self._orders.get(order_id)
        if order is None:
            raise OrderNotFound(order_id)
        return order

    async def pay(self, order_id: OrderId, amount_cents: int) -> Order:
        order = await self.get(order_id)
        order.assert_amount_matches(amount_cents)

        receipt = await self._payments.charge(order.customer_id, amount_cents)
        order.mark_paid(receipt.id)

        await self._orders.save(order)
        return order
```

## 5. `repositories/order_repository.py`

```python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.order import CustomerId, Order, OrderId, OrderStatus
from infra.models import OrderRow


class SqlOrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, order_id: OrderId) -> Order | None:
        row = await self._session.scalar(select(OrderRow).where(OrderRow.id == order_id))
        return _to_entity(row) if row else None

    async def save(self, order: Order) -> None:
        row = await self._session.get(OrderRow, order.id)
        if row is None:
            self._session.add(_to_row(order))
            return
        row.status = order.status.value
        row.receipt_id = order.receipt_id
        row.paid_at = order.paid_at


def _to_entity(row: OrderRow) -> Order:
    return Order(
        id=OrderId(row.id),
        customer_id=CustomerId(row.customer_id),
        amount_cents=row.amount_cents,
        status=OrderStatus(row.status),
        receipt_id=row.receipt_id,
        paid_at=row.paid_at,
    )
```

Note that the class **does not inherit** from `OrderRepository` — the `Protocol` is
satisfied structurally. mypy checks it at the injection site in `deps.py`.

The `row → entity` mapping lives in an explicit private function. Using the
SQLAlchemy model as the domain entity saves 20 lines today and costs the entire
decoupling later: lazy loading fires queries inside the manager, and
`DetachedInstanceError` shows up in places that are impossible to explain.

The repository **does not commit**. Transaction control belongs to the session
dependency, so that the whole use case is atomic.

## 6. `api/schemas/order.py`

```python
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field

from domain.order import OrderStatus


class PayOrderIn(BaseModel):
    amount_cents: int = Field(gt=0, description="Amount in cents")


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    customer_id: str
    amount_cents: int
    status: OrderStatus
    paid_at: datetime | None = None
```

`from_attributes=True` allows `OrderOut.model_validate(order)` straight from the
dataclass. The output schema is explicit instead of returning the entity: a new
domain field does not silently leak into the public API contract.

## 7. `api/deps.py`

```python
from collections.abc import AsyncIterator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from infra.db import session_factory
from infra.payments import StripeGateway
from managers.order_manager import OrderManager
from repositories.order_repository import SqlOrderRepository


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        async with session.begin():      # commit on success, rollback on exception
            yield session


def get_order_manager(session: AsyncSession = Depends(get_session)) -> OrderManager:
    return OrderManager(orders=SqlOrderRepository(session), payments=StripeGateway())
```

`session.begin()` as a context manager is what makes the use case atomic: if the
manager raises halfway through, nothing was persisted.

## 8. `api/routes/orders.py`

```python
from fastapi import APIRouter, Depends, status

from api.deps import get_order_manager
from api.schemas.order import OrderOut, PayOrderIn
from domain.order import OrderId
from managers.order_manager import OrderManager

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(
    order_id: str,
    manager: OrderManager = Depends(get_order_manager),
) -> OrderOut:
    return OrderOut.model_validate(await manager.get(OrderId(order_id)))


@router.post("/{order_id}/pay", response_model=OrderOut, status_code=status.HTTP_200_OK)
async def pay_order(
    order_id: str,
    payload: PayOrderIn,
    manager: OrderManager = Depends(get_order_manager),
) -> OrderOut:
    order = await manager.pay(OrderId(order_id), payload.amount_cents)
    return OrderOut.model_validate(order)
```

## 9. `tests/unit/test_order_manager.py`

```python
import pytest

from domain.errors import AmountMismatch, OrderNotFound
from domain.order import CustomerId, Order, OrderId, OrderStatus
from domain.ports import Receipt
from managers.order_manager import OrderManager


class FakeOrderRepository:
    def __init__(self, orders: list[Order] | None = None) -> None:
        self._by_id = {o.id: o for o in orders or []}
        self.saved: list[Order] = []

    async def get(self, order_id: OrderId) -> Order | None:
        return self._by_id.get(order_id)

    async def save(self, order: Order) -> None:
        self.saved.append(order)


class FakePaymentGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def charge(self, customer_id: CustomerId, amount_cents: int) -> Receipt:
        self.calls.append((customer_id, amount_cents))
        return Receipt(id="rc_1", amount_cents=amount_cents)


@pytest.fixture
def order() -> Order:
    return Order(id=OrderId("o1"), customer_id=CustomerId("c1"), amount_cents=1000)


async def test_pay_marks_as_paid_and_persists(order: Order) -> None:
    repo = FakeOrderRepository([order])
    gateway = FakePaymentGateway()
    manager = OrderManager(orders=repo, payments=gateway)

    result = await manager.pay(OrderId("o1"), 1000)

    assert result.status is OrderStatus.PAID
    assert result.receipt_id == "rc_1"
    assert repo.saved == [result]


async def test_pay_with_mismatched_amount_does_not_charge(order: Order) -> None:
    gateway = FakePaymentGateway()
    manager = OrderManager(orders=FakeOrderRepository([order]), payments=gateway)

    with pytest.raises(AmountMismatch):
        await manager.pay(OrderId("o1"), 999)

    assert gateway.calls == []      # the point: the customer was not charged


async def test_pay_unknown_order() -> None:
    manager = OrderManager(orders=FakeOrderRepository(), payments=FakePaymentGateway())

    with pytest.raises(OrderNotFound):
        await manager.pay(OrderId("nope"), 1000)
```

No `mock.patch`, no database, no network — and the tests run in milliseconds. That
is a direct consequence of the manager receiving `Protocol`s in `__init__`. The fake
records what it received (`saved`, `calls`), which enables assertions about side
effects that do not appear in the return value — such as "did not charge the
customer", which is precisely the test that matters.

Configuration note: the `async` tests above assume `asyncio_mode = "auto"` in
pytest; otherwise each needs `@pytest.mark.asyncio`.
