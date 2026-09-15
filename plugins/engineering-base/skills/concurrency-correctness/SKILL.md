---
name: concurrency-correctness
description: Correctness when two things happen at once — lost updates, double charges, duplicate rows, database deadlocks, idempotency, and transaction design. Use ALWAYS when code reads state and then writes it back (check-then-act), when writing anything that charges, transfers, decrements stock, assigns a unique value or consumes a queue message, and when handling retries or webhooks. Trigger on symptoms of wrong results under concurrency: "the customer was charged twice", "we got two rows with the same email", "deadlock detected", "the balance is wrong", "an update disappeared", "it only fails in production", "this is not reproducible locally", serialization failure, SQLSTATE 40001 or 40P01, `SELECT FOR UPDATE`, isolation level. This skill is about correct results, not speed — problems about latency, hanging or the event loop belong to the stack's async skill instead.
---

# Concurrency correctness

## Scope

This skill covers what goes wrong when two *separate* executions touch the same
state: two requests, two pods, two queue consumers. The shared thing is the
database or an external system, so the reasoning is the same in any language.

In-process concurrency — threads, the event loop, shared objects in memory — is
highly language-specific and belongs to the stack's own skill (`fastapi-async` for
Python). The division that matters:

| What is shared | Where the fix lives |
|---|---|
| memory inside one process | the stack's async skill — GIL, event loop, goroutines |
| **the database** | **here** |
| **state across processes, queues, external APIs** | **here** |

In a typical web service, almost every real concurrency bug is in the bottom two
rows. A single-threaded event loop rarely produces the textbook two-mutex
deadlock; two HTTP requests hitting the same row constantly produce lost updates.

## The shape of nearly every bug

```python
order = await repo.get(order_id)        # both requests read status="pending"
if order.status == "paid":              # both pass the check
    raise AlreadyPaid()
await gateway.charge(...)               # the customer is charged twice
await repo.save(order)
```

Read, decide, write — with a gap in the middle where someone else can act. The
check passed, and it was still wrong by the time it was used. Every variant of
this is the same bug: double charge, stock going negative, duplicate signup, two
workers grabbing the same job.

It is invisible in development because you are one user. It appears in production
under load, and it does not reproduce when you go looking for it.

## The four fixes, in order of preference

Prefer the earliest one that expresses your rule. Each later option costs more and
gives you more room to get it wrong.

**1. Let the database do it in one statement.** No gap, no lock to manage:

```sql
UPDATE accounts SET balance = balance - :amount
WHERE id = :id AND balance >= :amount
```

Then **check the affected row count**. Zero rows means the condition failed —
insufficient balance — and that is your business error. The check and the write are
one atomic operation, so there is no window between them.

This is the best option and the most often missed, because the intuitive code is
"read the balance, check it in Python, then write".

**2. A unique constraint.** For "this must exist only once", let the database be
the authority and handle the violation:

```sql
INSERT INTO subscriptions (user_id, plan) VALUES (:user, :plan)
ON CONFLICT (user_id) DO NOTHING
RETURNING id
```

Application-level "check if it exists, then insert" cannot be made correct without
a lock — there is always a gap. A unique index has no gap. `get_or_create` written
naively is this bug, and it is extremely common.

**3. `SELECT ... FOR UPDATE`.** When you must read, compute something in the
application, and then write:

```python
async with session.begin():
    order = await session.scalar(
        select(OrderRow).where(OrderRow.id == order_id).with_for_update()
    )
    if order.status == "paid":
        raise AlreadyPaid(order_id)
    order.status = "paid"
```

The row is locked until the transaction ends; the second request blocks and then
sees the committed result. Two rules: it only works **inside** a transaction, and
the lock is held until commit — so do not put a slow external call between the
`FOR UPDATE` and the commit (see pool exhaustion below).

**4. Optimistic locking with a version column.** When conflicts are rare and the
think time is long — editing a form, a long-running calculation:

```sql
UPDATE orders SET status = :status, version = version + 1
WHERE id = :id AND version = :expected_version
```

Zero rows affected means someone else changed it first. You then decide: retry, or
tell the user their view is stale. This avoids holding a lock, at the price of
handling the conflict.

## Isolation levels, and what they do not do

Postgres defaults to **READ COMMITTED**. It is worth knowing precisely what that
buys you, because the name suggests more safety than it provides:

| | READ COMMITTED (default) | REPEATABLE READ | SERIALIZABLE |
|---|---|---|---|
| Dirty read | prevented | prevented | prevented |
| Non-repeatable read | **possible** | prevented | prevented |
| Lost update | **possible** | error, retry | error, retry |
| Write skew | **possible** | **possible** | prevented |

The important part is what happens at the higher levels: they do not make the
problem disappear, they **convert silent corruption into an error you must
handle**. Under REPEATABLE READ, a conflicting transaction fails with a
serialization error (SQLSTATE 40001) and your code has to retry it. Raising the
isolation level without adding retry logic trades a data bug for an availability
bug.

Write skew is the one that surprises people: two transactions each read a
consistent snapshot, each check a condition that holds, and each write a different
row — leaving an invariant that spans rows broken. The classic case is "at least
one person must be on call": two people each check that the other is on call, and
both go off call. Only SERIALIZABLE prevents it; otherwise enforce the invariant
in a single row or with an explicit lock.

## Deadlocks

Two transactions each holding a lock the other needs. Postgres detects it, kills
one, and returns SQLSTATE **40P01**. Nothing is corrupted — but the killed
transaction failed, and someone has to deal with that.

**Prevention: update rows in a consistent order.** A deadlock needs inconsistent
ordering, so remove it:

```python
# transfer(a, b) and transfer(b, a) running at once will deadlock
for account_id in sorted([from_id, to_id]):        # always ascending
    await lock_and_update(account_id)
```

**Treatment: retry is part of the design, not error handling.** A deadlock or
serialization failure is expected under load, and both are safe to retry because
the transaction was rolled back whole:

```python
RETRYABLE = {"40001", "40P01"}   # serialization failure, deadlock detected

async def with_retry(operation, attempts: int = 3):
    for attempt in range(attempts):
        try:
            return await operation()
        except DBAPIError as exc:
            if exc.orig.sqlstate not in RETRYABLE or attempt == attempts - 1:
                raise
            await asyncio.sleep((2 ** attempt) * 0.05 * (1 + random.random()))
```

Retry the **whole transaction**, never part of it — the rollback undid everything,
so resuming halfway writes from a state that no longer exists. And add jitter: if
every retry waits exactly the same time, the conflicting transactions collide again
in lockstep.

## Idempotency

In any distributed path, the same operation will arrive twice. Not might: a retry
after a timeout, a webhook redelivered, a queue message re-consumed. The sender
cannot tell "failed" from "succeeded but the response was lost", so it retries.

The fix is to make a second execution harmless, with the database as the authority:

```python
async def charge(idempotency_key: str, customer_id: str, amount_cents: int):
    existing = await repo.get_by_key(idempotency_key)
    if existing is not None:
        return existing                      # replay the stored result

    receipt = await gateway.charge(customer_id, amount_cents)
    await repo.insert_charge(idempotency_key, receipt)   # UNIQUE on the key
    return receipt
```

The `UNIQUE` constraint on the key is what makes it real — the initial lookup is an
optimization, and on its own it is a check-then-act with the same gap as every
other one. When the insert violates the constraint, read the existing row and
return it.

**Queue consumers: at-least-once means duplicates will happen.** Do not try to
build exactly-once delivery; make the handler idempotent instead. And watch the
visibility timeout: if processing takes longer than it, the broker hands the same
message to a second worker while the first is still running — two workers, same
message, no error anywhere.

**Events and state must be written together.** This is the outbox pattern:

```python
async with session.begin():
    order.mark_paid(receipt.id)
    session.add(OutboxRow(topic="order.paid", payload=...))   # same transaction
# a separate process reads the outbox and publishes
```

Publishing to a broker from inside application code, after the commit, means a
crash in between loses the event; publishing before the commit means the event
fires for a change that got rolled back. The outbox makes both impossible because
the state change and the intent to publish commit atomically.

## Holding locks and connections too long

The failure that looks like starvation but is not:

```python
async with session.begin():
    order = await repo.get_for_update(order_id)
    receipt = await gateway.charge(...)       # 2 seconds of external HTTP
    order.mark_paid(receipt.id)
```

The row lock and the pooled connection are both held for the whole external call.
With a pool of 10 and a 2-second gateway, 10 concurrent requests exhaust the pool
and everything else queues until it times out — including requests that have
nothing to do with payments.

**Never do external I/O inside a database transaction.** Restructure so the call
happens outside, and use an idempotency key so a retry is safe:

```python
receipt = await gateway.charge(...)          # outside, no locks held
async with session.begin():                  # short transaction
    order = await repo.get_for_update(order_id)
    order.mark_paid(receipt.id)
```

The same reasoning applies to long-running transactions generally: a transaction
open across many statements holds its locks the entire time, and in Postgres it
also blocks vacuum from cleaning up, which degrades the whole database rather than
just that request.

## Testing it

A test with one caller cannot catch any of this. The test has to run two real
transactions against a real database and prove the invariant survives:

```python
@pytest.mark.integration
async def test_concurrent_payment_charges_only_once(engine) -> None:
    async def pay() -> str | None:
        async with session_factory(bind=engine)() as session, session.begin():
            try:
                return (await OrderManager(SqlOrderRepository(session),
                                           gateway).pay(order_id, 1000)).receipt_id
            except AlreadyPaid:
                return None

    results = await asyncio.gather(pay(), pay(), return_exceptions=True)

    assert sum(1 for r in results if isinstance(r, str)) == 1
    assert gateway.charge_count == 1
```

Two sessions, not two calls on one session — one session is one connection, and the
second call would just wait for the first, proving nothing. See `python-testing`
for the fixture setup.

## Checklist

- No check-then-act on shared state without a constraint, a lock, or an atomic statement
- Conditional updates verify the affected row count instead of reading first
- Uniqueness is enforced by a unique index, never by a prior `SELECT`
- Deadlock and serialization errors (40P01, 40001) are retried with backoff and jitter
- Rows are updated in a consistent order across code paths
- No external HTTP call inside a transaction
- Anything a client can retry accepts an idempotency key
- Queue handlers are idempotent; processing finishes well inside the visibility timeout
- Events are written to an outbox in the same transaction as the state change
- At least one test runs two concurrent transactions against a real database

## References

- `references/postgres.md` — isolation level behavior, lock types, `FOR UPDATE`
  variants, reading `pg_locks`, and diagnosing a deadlock from the log. Read when
  choosing a locking strategy or investigating a real deadlock.
- `references/distributed.md` — idempotency keys, the outbox pattern, distributed
  locks and why they are usually the wrong answer, broker delivery semantics. Read
  when the state is shared across processes rather than inside one database.
