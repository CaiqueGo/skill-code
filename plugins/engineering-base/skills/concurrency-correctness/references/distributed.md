# Concurrency across processes

When the shared state is not one database, the guarantees get weaker and the
patterns change. The decision guide is in `SKILL.md`; this covers what to do when a
single transaction can no longer cover the whole operation.

## Contents

- [The rule that replaces transactions](#the-rule-that-replaces-transactions)
- [Idempotency keys](#idempotency-keys)
- [The outbox pattern](#the-outbox-pattern)
- [Broker delivery semantics](#broker-delivery-semantics)
- [Distributed locks, and why usually not](#distributed-locks-and-why-usually-not)
- [Leader election and scheduled jobs](#leader-election-and-scheduled-jobs)
- [Retries that make things worse](#retries-that-make-things-worse)

## The rule that replaces transactions

Across a network you cannot have atomicity. Two writes to two systems will
sometimes leave one done and the other not — the process can die between them, and
no amount of ordering removes that window.

So the design rule is: **exactly one system is the source of truth, and every other
effect is made repeatable.** Rather than trying to make the pair atomic, make
re-running the second half harmless.

That single idea produces idempotency keys, the outbox, and idempotent consumers.
They are all the same move applied at different points.

## Idempotency keys

The caller generates a key — a UUID — and sends it with the request. The same
logical operation retried carries the same key.

```sql
CREATE TABLE charges (
    idempotency_key TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL,
    amount_cents    INT  NOT NULL,
    receipt_id      TEXT,
    status          TEXT NOT NULL,          -- in_progress | succeeded | failed
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

The order of operations matters more than it looks:

```python
async def charge(key: str, customer_id: str, amount_cents: int) -> Receipt:
    # 1. claim the key BEFORE doing anything external
    try:
        await repo.insert_in_progress(key, customer_id, amount_cents)
    except UniqueViolation:
        existing = await repo.get(key)
        if existing.status == "in_progress":
            raise ChargeInFlight(key)        # 409: retry later, do not duplicate
        return existing.receipt              # replay the stored result

    # 2. only now the external call
    receipt = await gateway.charge(customer_id, amount_cents, idempotency_key=key)

    # 3. record the outcome
    await repo.mark_succeeded(key, receipt)
    return receipt
```

Claiming the key first is what closes the window. If you call the gateway first and
insert afterwards, two concurrent requests with the same key both reach the gateway
before either has inserted — the exact thing the key was supposed to prevent.

The `in_progress` state exists because the process can die at step 2. You then have
a row with no receipt and no way to know locally whether the money moved — so
reconcile against the provider rather than guessing. This is why gateways offer
their own idempotency key: pass yours through, and their retry is safe too.

Scope the key to the operation, not to the user, and keep the rows long enough to
outlive any client retry window — days, not minutes.

## The outbox pattern

Changing state and publishing an event must not be two independent writes:

```python
# WRONG: crash between them loses the event permanently
async with session.begin():
    order.mark_paid(receipt.id)
await broker.publish("order.paid", payload)

# ALSO WRONG: publishes an event for a change that may roll back
await broker.publish("order.paid", payload)
async with session.begin():
    order.mark_paid(receipt.id)
```

Write the intent to publish in the same transaction as the state change:

```python
async with session.begin():
    order.mark_paid(receipt.id)
    session.add(OutboxRow(
        topic="order.paid",
        payload=json.dumps({"order_id": order.id, "receipt_id": receipt.id}),
    ))
```

A separate process drains it:

```sql
UPDATE outbox SET published_at = now()
WHERE id IN (
    SELECT id FROM outbox WHERE published_at IS NULL
    ORDER BY id LIMIT 100
    FOR UPDATE SKIP LOCKED
)
RETURNING *;
```

`SKIP LOCKED` lets several publishers run without stepping on each other. The
publisher will occasionally publish twice — it can die after the broker accepted
the message and before the row was marked — which is fine, because consumers are
idempotent. That is the trade: you get at-least-once and no lost events, rather
than exactly-once, which is not available.

Include the event id in the payload so consumers can deduplicate on it.

## Broker delivery semantics

| Broker | Default | What actually bites |
|---|---|---|
| SQS standard | at-least-once | duplicates, and out-of-order delivery |
| SQS FIFO | exactly-once *within a dedup window* | 5-minute window, per message group |
| RabbitMQ | at-least-once with ack | redelivery after a consumer dies |
| Kafka | at-least-once | rebalance replays from the last committed offset |

**Visibility timeout is the detail that produces mysterious duplicates.** If
processing takes longer than the timeout, the broker decides the consumer died and
gives the message to a second one — while the first is still working. Two consumers,
same message, no error anywhere in the logs. Either keep processing well under the
timeout or extend the lease while working.

Design the handler so a duplicate is a no-op:

```python
async def handle(event: dict) -> None:
    async with session.begin():
        try:
            session.add(ProcessedEvent(event_id=event["id"]))   # PRIMARY KEY
            await session.flush()
        except UniqueViolation:
            return          # already handled; ack and move on

        await apply(event)  # the effect commits with the marker, atomically
```

Marker and effect in one transaction is the point. Marking first and applying
afterwards loses the effect if the process dies in between.

## Distributed locks, and why usually not

A lock in Redis looks like the obvious answer and is very hard to make correct. The
problem is not the implementation — it is that a lock has a TTL, and a process that
pauses (GC, a slow disk, a rescheduled pod) can hold a lock it believes is still
valid long after it expired. Two processes then both act while each believes it is
alone, and nothing detects it.

Before reaching for one, try in this order:

1. **A database constraint.** Uniqueness enforced by an index needs no lock and
   cannot go stale.
2. **A single-statement conditional update.** `UPDATE ... WHERE status = 'pending'`
   plus a row-count check is an atomic claim.
3. **`FOR UPDATE SKIP LOCKED`** to hand work out — see `postgres.md`.
4. **A Postgres advisory lock**, if you genuinely need a named lock. It is tied to
   the transaction and disappears when the connection dies, so it cannot outlive its
   holder.

If you still need Redis — usually for coordinating something outside the database —
then require that the protected operation is **idempotent anyway**, so a
double-execution is survivable. A lock you cannot trust is a performance
optimization, not a correctness mechanism, and should be treated as one.

## Leader election and scheduled jobs

A cron job on three replicas runs three times. The cheapest fix is a claim row
rather than a coordination service:

```sql
INSERT INTO job_runs (job_name, run_at) VALUES ('daily_report', date_trunc('hour', now()))
ON CONFLICT (job_name, run_at) DO NOTHING
RETURNING id;
```

Zero rows returned means another replica claimed this run — exit quietly. The
unique constraint on `(job_name, run_at)` is the entire mechanism, and it is correct
without a lock, a TTL, or a heartbeat.

## Retries that make things worse

A retry without an idempotency key is a duplicate generator, and a retry without
backoff is a load amplifier. When a downstream service is degraded, every client
retrying immediately multiplies traffic exactly when it can least absorb it —
which keeps it down.

- **Never retry a non-idempotent operation** unless it carries an idempotency key.
- **Exponential backoff with jitter.** Without jitter, everyone retries in lockstep
  and the thundering herd repeats at each interval.
- **Cap total attempts and total elapsed time.** Retrying for three minutes on a
  request whose client gave up after five seconds is pure waste.
- **Do not retry a 4xx.** The request is wrong; it will be wrong again.
- **A circuit breaker** stops retrying entirely once a downstream is clearly down,
  which is what lets it recover.
- **Retry the outermost operation, not each layer.** Three layers each retrying
  three times is 27 requests for one logical call — this compounds silently and is
  usually discovered during an incident.
