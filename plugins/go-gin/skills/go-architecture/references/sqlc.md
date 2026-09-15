# sqlc reference

sqlc reads your schema and your queries and generates typed Go. You keep writing
SQL; it removes the hand-written scanning. The decision of where the generated code
sits is in `SKILL.md` — this is how to drive it.

## Contents

- [Configuration](#configuration)
- [Query annotations](#query-annotations)
- [Nullability](#nullability)
- [Transactions](#transactions)
- [Bulk operations](#bulk-operations)
- [Dynamic filters](#dynamic-filters)
- [Migrations and the regeneration loop](#migrations-and-the-regeneration-loop)
- [Common mistakes](#common-mistakes)

## Configuration

```yaml
# sqlc.yaml
version: "2"
sql:
  - engine: postgresql
    schema: db/migrations          # sqlc reads the migrations to learn the schema
    queries: db/query
    gen:
      go:
        package: sqlc
        out: db/sqlc
        sql_package: pgx/v5        # or database/sql
        emit_interface: true       # generates a Querier interface
        emit_json_tags: false      # generated rows are not your API shape
        emit_pointers_for_null_types: true
        emit_empty_slices: true    # :many returns [] instead of nil
        overrides:
          - db_type: "uuid"
            go_type: "github.com/google/uuid.UUID"
          - db_type: "timestamptz"
            go_type: "time.Time"
```

The settings worth understanding rather than copying:

- **`sql_package: pgx/v5`** — pgx is the better Postgres driver and gives you
  `pgxpool`. It changes generated signatures, so decide before writing code.
- **`emit_interface: true`** generates `Querier` with every method. Useful mostly as
  a compile-time check that a transaction wrapper is complete; your service should
  still declare its own narrow interface rather than depend on `Querier`, which
  grows with every query anyone adds.
- **`emit_empty_slices: true`** — without it a `:many` with no rows returns `nil`,
  which serializes to JSON `null` instead of `[]`, and breaks clients.
- **`emit_json_tags: false`** — generated rows are not your API contract. Tagging
  them invites returning them directly from a handler.
- **`emit_pointers_for_null_types: true`** — `*string` instead of
  `sql.NullString`. Much easier to map, and the nil check is the usual Go one.

`schema:` pointing at the migrations directory is what keeps generated code and the
real database in step: the schema sqlc knows is exactly the one migrations produce.

## Query annotations

```sql
-- db/query/order.sql

-- name: GetOrder :one
SELECT * FROM orders WHERE id = $1;

-- name: ListOrdersByCustomer :many
SELECT * FROM orders
WHERE customer_id = $1 AND created_at > $2
ORDER BY created_at DESC
LIMIT $3 OFFSET $4;

-- name: MarkOrderPaid :exec
UPDATE orders SET status = 'paid', receipt_id = $2, paid_at = now()
WHERE id = $1;

-- name: MarkOrderPaidIfPending :execrows
UPDATE orders SET status = 'paid', receipt_id = $2, paid_at = now()
WHERE id = $1 AND status = 'pending';

-- name: CreateOrder :one
INSERT INTO orders (id, customer_id, amount_cents)
VALUES ($1, $2, $3)
RETURNING *;
```

| Annotation | Returns | Use for |
|---|---|---|
| `:one` | row, `sql.ErrNoRows` if none | fetch by id |
| `:many` | slice | listings |
| `:exec` | error only | writes where you do not care how many |
| `:execrows` | rows affected | **conditional updates** |
| `:execlastid` | last insert id | MySQL auto-increment |
| `:copyfrom` | error | bulk insert |
| `:batchexec` | batch | many writes, one round trip |

**`:execrows` is the one to reach for more often than people do.** It is how a
conditional update reports whether the condition held —
`MarkOrderPaidIfPending` returning 0 means someone else already paid it. That makes
check-and-write a single atomic statement instead of a read followed by a write
with a gap in between (see `concurrency-correctness`).

Prefer naming columns over `SELECT *` in queries you return to callers: with `*`,
adding a column to the table silently changes the generated struct, and anything
that constructs it positionally breaks.

## Nullability

sqlc derives nullability from the schema, and it is stricter than people expect.

```sql
CREATE TABLE orders (
    id          TEXT PRIMARY KEY,
    receipt_id  TEXT,                -- nullable -> *string
    amount_cents BIGINT NOT NULL     -- -> int64
);
```

A `LEFT JOIN` makes every column from the joined table nullable, even columns
declared `NOT NULL` — correctly, since the join may produce no match. If the
generated struct has pointers you did not expect, look for the join.

Aggregates are nullable too: `SUM(amount)` over zero rows is `NULL`, so it
generates a pointer or an `interface{}`. Wrap it to keep the type clean:

```sql
-- name: TotalByCustomer :one
SELECT COALESCE(SUM(amount_cents), 0)::bigint AS total
FROM orders WHERE customer_id = $1;
```

The `::bigint` cast matters: without it sqlc cannot infer the type of the
`COALESCE` result and generates `interface{}`.

## Transactions

The generated `Queries` has `WithTx`, which is how a use case spanning several
writes stays atomic:

```go
func (s *Store) WithTx(ctx context.Context, fn func(*Store) error) error {
    tx, err := s.pool.Begin(ctx)
    if err != nil {
        return fmt.Errorf("begin: %w", err)
    }
    defer tx.Rollback(ctx)      // no-op after a successful commit

    if err := fn(&Store{q: s.q.WithTx(tx), pool: s.pool}); err != nil {
        return err              // the deferred rollback undoes everything
    }
    return tx.Commit(ctx)
}
```

`defer tx.Rollback(ctx)` immediately after `Begin` is the pattern: it fires on every
return path, including a panic, and after a successful `Commit` it is a harmless
no-op. Writing rollbacks in each error branch means one branch eventually forgets,
and that connection leaks until the pool is exhausted.

Used from the service:

```go
err := s.store.WithTx(ctx, func(st *Store) error {
    if err := st.MarkPaid(ctx, arg); err != nil {
        return err
    }
    return st.InsertOutbox(ctx, event)
})
```

**Never make an HTTP call inside the callback.** The transaction holds a pooled
connection and its row locks for the whole call — see `concurrency-correctness`.

## Bulk operations

```sql
-- name: InsertOrderItems :copyfrom
INSERT INTO order_items (order_id, sku, qty) VALUES ($1, $2, $3);
```

`:copyfrom` uses the Postgres COPY protocol — dramatically faster than a loop of
inserts, and the right choice above a few hundred rows. Restrictions: no
`RETURNING`, no `ON CONFLICT`. When you need either, use `:batchexec`, which pipelines
many statements into one round trip.

## Dynamic filters

sqlc generates from static SQL, so an optional filter cannot be an `if` in Go. Use
the null-argument pattern instead of building strings:

```sql
-- name: SearchOrders :many
SELECT * FROM orders
WHERE customer_id = $1
  AND (sqlc.narg('status')::text IS NULL OR status = sqlc.narg('status'))
  AND (sqlc.narg('min_cents')::bigint IS NULL OR amount_cents >= sqlc.narg('min_cents'))
ORDER BY created_at DESC
LIMIT $2;
```

`sqlc.narg()` makes the parameter nullable; passing nil disables that condition.
This stays a single prepared statement, so it is injection-proof and the plan is
cached.

When the shape genuinely varies — sorting by a user-chosen column, arbitrary filter
combinations — do not concatenate. Map the allowed values explicitly:

```go
var sortColumns = map[string]string{"created_at": "created_at", "amount": "amount_cents"}
col, ok := sortColumns[req.SortBy]
if !ok {
    return ErrInvalidSort
}
```

An allowlist means user input never reaches the SQL text, only a value you wrote.

## Migrations and the regeneration loop

sqlc does not run migrations; it reads them. Pair it with `goose`,
`golang-migrate` or `atlas`.

```
db/migrations/
  20260915093000_create_orders.sql
  20260915141200_add_receipt_id.sql
```

```sql
-- +goose Up
ALTER TABLE orders ADD COLUMN receipt_id TEXT;

-- +goose Down
ALTER TABLE orders DROP COLUMN receipt_id;
```

The loop, which belongs in the Makefile (see `dev-environment`):

```make
migrate-new:   ## create a migration: make migrate-new name=add_receipt_id
	goose -dir db/migrations create $(name) sql

migrate-up:
	goose -dir db/migrations postgres "$(DATABASE_URL)" up

generate:      ## regenerate sqlc after any schema or query change
	sqlc generate

db-reset: migrate-up generate
```

**Regeneration is not optional and it is easy to forget.** Change the schema without
running `sqlc generate` and the generated structs describe a database that no longer
exists — code compiles, queries fail at runtime. Guard it in CI:

```yaml
- run: sqlc diff        # fails if generated code is out of date
- run: sqlc vet         # runs queries through EXPLAIN against the schema
```

`sqlc vet` catches a query that parses but cannot execute — a missing column, a bad
cast — at build time instead of on the first request.

## Common mistakes

- **Editing generated files.** They are overwritten on the next `generate`. If
  something is missing, change the query.
- **`Queries` reaching the service.** The service then depends on all forty methods
  and cannot be faked in two lines. Keep the narrow interface.
- **Not translating `sql.ErrNoRows`.** It leaks `database/sql` into every caller.
- **Forgetting `emit_empty_slices`.** `:many` with no rows becomes JSON `null`.
- **`SELECT *` in queries whose result is returned to a caller.** Adding a column
  changes the struct under you.
- **Assuming a `LEFT JOIN` keeps `NOT NULL`.** It does not, and the pointer types
  are correct.
- **Building SQL strings for dynamic filters.** Use `sqlc.narg` or an allowlist.
