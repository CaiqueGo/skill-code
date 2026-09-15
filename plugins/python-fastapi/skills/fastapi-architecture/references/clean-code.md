# Clean code in Python — what actually changes readability

A day-to-day reference. This is not a style list: every item exists because its
absence has already caused a bug or rework in a production service.

## Contents

- [Names](#names)
- [Functions](#functions)
- [Typing](#typing)
- [Errors](#errors)
- [Configuration](#configuration)
- [Logging](#logging)
- [Tooling and minimal configuration](#tooling-and-minimal-configuration)

## Names

A name carries the unit and the type whenever they are ambiguous. Is `timeout` in
seconds or milliseconds? Is `amount` in dollars or cents? That doubt becomes a
production bug:

```python
# bad
def charge(customer, amount, timeout): ...

# good
def charge(customer_id: str, amount_cents: int, timeout_seconds: float) -> Receipt: ...
```

A boolean states the condition, not the question: `is_active`, `has_expired`,
`should_retry`. Avoid negation in the name (`is_not_valid`) — reading
`not is_not_valid` is expensive.

Collections plural, items singular. `for order in orders`, never `for o in order_list`.

Do not abbreviate what is not universal. `db`, `id`, `url`, `http` are universal.
`ordr`, `mgr_svc`, `calc_tot` are not.

## Functions

**Length is not the metric; number of abstraction levels is.** A 40-line function
that only assembles a dictionary is readable. A 12-line one mixing business rules,
a query and formatting is not.

A boolean argument is a sign that there are two functions hiding in there:

```python
# bad — a reader of the call site cannot tell what True means
await manager.export(order_id, True)

# good
await manager.export_as_pdf(order_id)
await manager.export_as_csv(order_id)

# or, when they really are variations of one flow, force a keyword
async def export(self, order_id: str, *, include_taxes: bool = False) -> bytes: ...
```

`*` forces keyword arguments. Use it whenever a function takes more than two
parameters of the same type — it prevents silent positional swaps, which the type
checker cannot catch.

Return early instead of nesting:

```python
# bad
def process(order):
    if order is not None:
        if order.is_valid():
            if not order.is_paid:
                return do_work(order)
    return None

# good
def process(order: Order | None) -> Result | None:
    if order is None:
        return None
    if not order.is_valid():
        return None
    if order.is_paid:
        return None
    return do_work(order)
```

A mutable default is the classic trap — the default is created once, at definition
time:

```python
def add(item: str, bucket: list[str] = []) -> list[str]:  # NEVER
def add(item: str, bucket: list[str] | None = None) -> list[str]:
    bucket = [] if bucket is None else bucket
```

## Typing

Typing in Python is not decoration: it is what lets you refactor without fear, and
it replaces half of your contract tests. Run `mypy --strict` on `domain/` and
`managers/` at minimum — those are the pure layers, where it is cheap.

```python
# `Any` is a decision, not a default. When you use it, say why.
def parse(payload: dict[str, Any]) -> Order: ...

# modern union; no legacy Optional/Union
def find(order_id: str) -> Order | None: ...

# generics where the type genuinely varies
def first[T](items: Sequence[T]) -> T | None:
    return items[0] if items else None
```

`NewType` for IDs prevents the single most common mistake — passing `customer_id`
where `order_id` was expected, both of them `str`:

```python
from typing import NewType

OrderId = NewType("OrderId", str)
CustomerId = NewType("CustomerId", str)
```

A domain entity is a dataclass with behavior, not a `dict`:

```python
from dataclasses import dataclass
from enum import Enum


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

    def mark_paid(self, receipt_id: str) -> None:
        if self.status is not OrderStatus.PENDING:
            raise InvalidTransition(self.status, OrderStatus.PAID)
        self.status = OrderStatus.PAID
        self.receipt_id = receipt_id
```

`slots=True` cuts memory and rejects assignment to nonexistent attributes — a typo
in `order.stat = ...` becomes an error instead of a phantom field.

## Errors

One hierarchy per domain, with a stable code for the API client:

```python
# domain/errors.py
class DomainError(Exception):
    code: str = "domain_error"


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    code = "conflict"


class OrderNotFound(NotFoundError):
    code = "order_not_found"

    def __init__(self, order_id: str) -> None:
        super().__init__(f"order {order_id} not found")
        self.order_id = order_id
```

The `code` exists because the message changes (translation, extra detail) and the
client cannot depend on free-form text. Store structured data on the error
(`self.order_id`) — the handler and the log then use it without parsing the message.

Never swallow an exception. `except Exception: pass` is the most efficient way to
make an incident undebuggable. If you must continue, log with `exc_info=True` and
say why.

Catch as narrowly as possible, at the level that knows what to do. `except Exception`
belongs only in the global handler.

## Configuration

All configuration comes from the environment, validated at startup — not scattered
as `os.getenv` throughout the code:

```python
# infra/settings.py
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_")

    database_url: str
    payment_api_key: str
    http_timeout_seconds: float = Field(default=5.0, gt=0)
    max_concurrent_calls: int = Field(default=10, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

If a variable is missing, the process dies at startup with a clear message — not on
the first request in production at 3am. `lru_cache` guarantees a single instance and
allows overriding in tests via `get_settings.cache_clear()`.

A secret never has a default. `payment_api_key: str = "changeme"` is exactly how a
test key reaches production.

## Logging

Structured logs, with context, no f-string in the message:

```python
import logging

logger = logging.getLogger(__name__)

# bad — impossible to aggregate, and it formats even when the level is off
logger.info(f"payment for order {order_id} failed with {err}")

# good — queryable fields
logger.warning("payment_failed", extra={"order_id": order_id, "reason": err.code})
```

Never log a token, password, card PAN, full national ID, or an entire request body.
If you need correlation, use a `request_id` propagated by middleware.

Right level: `DEBUG` for development, `INFO` for business events, `WARNING` for
recovered degradation, `ERROR` for failures requiring human action. If everything is
`ERROR`, nobody reads the alert.

## Tooling and minimal configuration

```toml
# pyproject.toml
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "S", "ASYNC", "RUF"]
# B = bugbear (real traps), S = bandit (security),
# ASYNC = event loop blocking, UP = modern syntax, I = import order

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["S101"]   # assert is expected in tests

[tool.mypy]
python_version = "3.12"
strict = true
plugins = ["pydantic.mypy"]

[[tool.mypy.overrides]]
module = ["tests.*"]
strict = false

[tool.importlinter]
root_package = "src"

[[tool.importlinter.contracts]]
name = "Dependencies point inward"
type = "layers"
layers = ["src.api", "src.managers", "src.domain"]
```

The `import-linter` contract is what turns the layering rule into a CI failure.
Without it, the architecture depends on someone remembering during code review — and
eventually nobody does.
