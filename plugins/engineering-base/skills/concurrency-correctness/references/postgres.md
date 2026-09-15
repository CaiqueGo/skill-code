# Postgres concurrency reference

Details for choosing a locking strategy and for diagnosing a real deadlock. The
decision guide is in `SKILL.md`; this is what you need once you have decided.

## Contents

- [Isolation levels in practice](#isolation-levels-in-practice)
- [Row locks](#row-locks)
- [FOR UPDATE variants](#for-update-variants)
- [Advisory locks](#advisory-locks)
- [Diagnosing a deadlock](#diagnosing-a-deadlock)
- [Seeing what is blocked right now](#seeing-what-is-blocked-right-now)
- [Common mistakes](#common-mistakes)

## Isolation levels in practice

```sql
-- per transaction
BEGIN ISOLATION LEVEL REPEATABLE READ;

-- or per session
SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL REPEATABLE READ;
```

In SQLAlchemy:

```python
engine = create_async_engine(url, isolation_level="REPEATABLE READ")

# or for one transaction
async with engine.connect() as conn:
    await conn.execution_options(isolation_level="SERIALIZABLE")
```

**READ COMMITTED** (default). Each *statement* sees a fresh snapshot. Two
statements in the same transaction can see different data — that is the
non-repeatable read, and it is why `SELECT` then `UPDATE` in one transaction is not
safe by itself.

Worth knowing: under READ COMMITTED, an `UPDATE` that finds a row locked by another
transaction waits, and when that transaction commits, the update **re-evaluates its
`WHERE` clause against the new version**. So `UPDATE ... WHERE balance >= 100`
behaves correctly under concurrency even at this level — which is exactly why the
single-statement fix is the preferred one.

**REPEATABLE READ.** The transaction sees one snapshot from its first statement.
Postgres implements this as snapshot isolation, which also prevents phantom reads
(stricter than the SQL standard requires). If your transaction tries to update a
row that changed after your snapshot, it fails immediately:

```
ERROR: could not serialize access due to concurrent update
SQLSTATE: 40001
```

That is not a bug to suppress — it is the level doing its job. Retry the whole
transaction.

**SERIALIZABLE.** Adds predicate locking to catch write skew. Same 40001 error on
conflict, more often. Use it when an invariant spans multiple rows and you cannot
express it as a constraint. Read-only transactions can be declared to avoid some of
the cost:

```sql
BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE READ ONLY DEFERRABLE;
```

At REPEATABLE READ or SERIALIZABLE, retry logic is mandatory. Without it you have
swapped silent data corruption for user-visible 500s.

## Row locks

| Lock | Taken by | Blocks |
|---|---|---|
| `FOR UPDATE` | explicit | other `FOR UPDATE`/`FOR SHARE`, and `UPDATE`/`DELETE` |
| `FOR NO KEY UPDATE` | plain `UPDATE` | `FOR UPDATE`, other updates |
| `FOR SHARE` | explicit | writers, not other readers |
| `FOR KEY SHARE` | foreign key checks | `FOR UPDATE` only |

Plain `SELECT` never blocks and is never blocked — Postgres uses MVCC, so readers
see an older version rather than waiting. This is why a long-running report does
not block writes, and also why a `SELECT` gives you no protection at all.

`FOR KEY SHARE` explains a surprising case: inserting a child row takes a key-share
lock on the parent, so a transaction doing `FOR UPDATE` on the parent blocks
inserts of children. If unrelated-looking inserts are blocking, this is usually why.

## FOR UPDATE variants

```sql
SELECT * FROM orders WHERE id = :id FOR UPDATE;              -- wait
SELECT * FROM orders WHERE id = :id FOR UPDATE NOWAIT;       -- error immediately
SELECT * FROM jobs WHERE status = 'pending'
  ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED;        -- skip locked rows
```

**`SKIP LOCKED` is how you build a work queue in Postgres.** Each worker takes a
different row instead of queueing behind the first one — several workers pull jobs
concurrently with no coordination and no broker:

```sql
UPDATE jobs SET status = 'running', worker = :worker
WHERE id = (
    SELECT id FROM jobs WHERE status = 'pending'
    ORDER BY created_at LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING *;
```

For moderate volume this is simpler and more reliable than adding a broker, because
the job state and your business data commit in the same transaction.

**`NOWAIT`** is for interactive paths where waiting is worse than failing: better to
tell the user "someone else is editing this" than to hold the request for 30
seconds.

In SQLAlchemy: `.with_for_update(skip_locked=True)`, `.with_for_update(nowait=True)`.

## Advisory locks

Locks on an arbitrary number rather than a row — for coordinating work that is not
one row, such as "only one instance runs this import at a time":

```sql
SELECT pg_advisory_xact_lock(:key);        -- released at commit; prefer this
SELECT pg_try_advisory_xact_lock(:key);    -- returns false instead of waiting
```

Prefer the `_xact_` variants: a session-level advisory lock survives until it is
explicitly released or the connection drops, and with a connection pool that
connection gets reused by an unrelated request that now silently holds the lock.

Derive the key deterministically (`hashtext('import:' || :tenant_id)`), and write
down the mapping somewhere — a bare number in the logs is unreadable during an
incident.

## Diagnosing a deadlock

Postgres logs the full picture. Make sure it is turned on:

```sql
ALTER SYSTEM SET log_lock_waits = on;
ALTER SYSTEM SET deadlock_timeout = '1s';   -- also the detection delay
SELECT pg_reload_conf();
```

A deadlock log entry names both transactions, the statement each was running, and
which lock each was waiting for:

```
ERROR:  deadlock detected
DETAIL:  Process 123 waits for ShareLock on transaction 456; blocked by process 789.
         Process 789 waits for ShareLock on transaction 457; blocked by process 123.
         Process 123: UPDATE accounts SET balance = ... WHERE id = 'a'
         Process 789: UPDATE accounts SET balance = ... WHERE id = 'b'
HINT:  See server log for query details.
```

Read the two statements and find the ordering difference. The fix is almost always
to make both paths touch the rows in the same order — not to raise a timeout.

Note that `deadlock_timeout` is how long Postgres waits before *checking* for a
deadlock, not a limit on lock waiting. Raising it does not reduce deadlocks; it
just delays detection.

## Seeing what is blocked right now

```sql
SELECT pid, state, wait_event_type, wait_event,
       now() - query_start AS duration, left(query, 80) AS query
FROM pg_stat_activity
WHERE state <> 'idle'
ORDER BY duration DESC;

-- who is blocking whom
SELECT blocked.pid AS blocked_pid, blocking.pid AS blocking_pid,
       left(blocked.query, 60) AS blocked_query,
       left(blocking.query, 60) AS blocking_query
FROM pg_stat_activity blocked
JOIN pg_stat_activity blocking ON blocking.pid = ANY(pg_blocking_pids(blocked.pid))
WHERE blocked.wait_event_type = 'Lock';
```

`idle in transaction` in `pg_stat_activity` is the state to look for during an
incident: a transaction was opened, is holding its locks, and the application is
doing something else — usually an external HTTP call. Set a guard so it cannot last
forever:

```sql
ALTER ROLE app SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE app SET lock_timeout = '10s';
ALTER ROLE app SET statement_timeout = '30s';
```

These three are the cheapest protection against one bad code path degrading the
whole database. `lock_timeout` in particular turns an indefinite pile-up into a
fast, visible error.

## Common mistakes

- **`SELECT FOR UPDATE` outside a transaction.** With autocommit, the lock is
  released immediately and it does nothing at all.
- **Locking after reading.** `SELECT`, then decide, then `SELECT FOR UPDATE` —
  the gap is still there. Lock on the first read, and re-check the condition after
  acquiring the lock.
- **Expecting `FOR UPDATE` to block inserts.** It locks existing rows; it cannot
  stop a new row that matches your criteria from appearing. For that you need a
  unique constraint or SERIALIZABLE.
- **Catching `IntegrityError` without rolling back.** In Postgres, a failed
  statement aborts the whole transaction — every subsequent statement returns
  "current transaction is aborted" until you roll back. Use a savepoint
  (`session.begin_nested()`) if you want to continue after handling it.
- **Raising the isolation level without adding retries.** Trades data corruption
  for 500s.
