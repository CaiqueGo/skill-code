---
name: go-testing
description: Testing Go services — table-driven tests, fakes over mocks, testify assertions, httptest and gin handler tests, sqlc repository tests against real Postgres with testcontainers, golden files, and what not to test. Use ALWAYS when writing, fixing or reviewing a Go test: `_test.go`, `t.Run`, `testing.T`, `httptest`, testcontainers, `go test`, table tests, test fixtures, coverage, "how do I test this handler", "how do I fake this interface", "this test is flaky", or new code that has no test yet. Trigger also whenever someone reaches for a mock-generation tool — in Go a hand-written fake is usually smaller and clearer.
---

# Testing Go services

## Three layers, three risks

What decides the kind of test is which risk it removes:

| Layer | Removes the risk that | Cost | Where |
|---|---|---|---|
| Unit | the business rule is wrong | µs, no I/O | `service_test.go` with fakes |
| Integration | the SQL is wrong | seconds, real Postgres | `store_test.go` with testcontainers |
| HTTP | the wiring is wrong | ms, in-process | `handler_test.go` with `httptest` |

Go makes the middle layer cheap in a way many languages do not: `go test` runs
packages in parallel, and a container started once per package is amortised across
every test in it.

Testing a business rule through HTTP costs more and fails for unrelated reasons.
Testing sqlc-generated queries with a fake tests nothing at all — the fake returns
whatever you told it to, while the thing that can actually be wrong is the SQL.

## Table-driven by default

This is the Go idiom, and it earns its place: adding a case is one line, and every
case reports its own name on failure.

```go
func TestOrder_MarkPaid(t *testing.T) {
	tests := []struct {
		name    string
		status  Status
		wantErr error
	}{
		{name: "pending becomes paid", status: StatusPending},
		{name: "already paid is rejected", status: StatusPaid, wantErr: ErrAlreadyPaid},
		{name: "cancelled is rejected", status: StatusCancelled, wantErr: ErrAlreadyPaid},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			o := Order{ID: "o1", Status: tt.status}

			err := o.MarkPaid("rc_1", time.Now())

			if !errors.Is(err, tt.wantErr) {
				t.Fatalf("MarkPaid() error = %v, want %v", err, tt.wantErr)
			}
		})
	}
}
```

Details that matter:

- **`t.Run` with a descriptive name** — failure output says
  `TestOrder_MarkPaid/already_paid_is_rejected`, which is readable in CI by
  someone who did not write it.
- **`errors.Is`, not `==`** — a wrapped error is not equal to its sentinel, and the
  service layer wraps.
- **`t.Fatalf` when continuing is pointless**, `t.Errorf` when you want the rest of
  the assertions to still run.
- **`t.Parallel()`** inside the subtest, when the cases share nothing.

## Fakes, not generated mocks

Go interfaces are satisfied structurally, so a fake is a small struct. Before
adding mockery or gomock, notice that the hand-written version is usually shorter
than the generated one and does not need a build step:

```go
type fakeStore struct {
	order    Order
	getErr   error
	affected int64
	calls    []string       // records what it received
}

func (f *fakeStore) GetOrder(_ context.Context, id string) (Order, error) {
	f.calls = append(f.calls, "GetOrder:"+id)
	return f.order, f.getErr
}

func (f *fakeStore) MarkPaidIfPending(_ context.Context, _, _ string, _ time.Time) (int64, error) {
	return f.affected, nil
}
```

The reason to prefer this is not brevity, it is that a fake **records what it
received**, which lets you assert on side effects that never appear in the return
value:

```go
if gateway.calls != 0 {
	t.Errorf("charged the customer on a mismatched amount")
}
```

"Did not charge the customer" is the assertion that matters in that test, and it is
far clearer than the mock equivalent.

Keep the interface narrow — declared where it is consumed (see `go-architecture`) —
and the fake stays small. A fake with forty methods means the interface is wrong.

Generated mocks earn their place when an interface is large and externally owned,
or when you need strict call-order verification. That is rare.

## Testing gin handlers

`httptest` runs the router in-process: no port, no server, no waiting.

```go
func TestHandler_Pay(t *testing.T) {
	gin.SetMode(gin.TestMode)      // silences gin's own logging

	tests := []struct {
		name       string
		body       string
		affected   int64
		wantStatus int
		wantCode   string
	}{
		{name: "ok", body: `{"amount_cents":1000}`, affected: 1, wantStatus: 200},
		{name: "bad body", body: `{"amount_cents":0}`, wantStatus: 400, wantCode: "invalid_body"},
		{name: "already paid", body: `{"amount_cents":1000}`, affected: 0,
			wantStatus: 409, wantCode: "order_already_paid"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			svc := NewService(
				&fakeStore{order: pendingOrder, affected: tt.affected},
				&fakeGateway{},
			)
			r := gin.New()
			NewHandler(svc).Register(r)

			req := httptest.NewRequest(http.MethodPost, "/orders/o1/pay",
				strings.NewReader(tt.body))
			req.Header.Set("Content-Type", "application/json")
			rec := httptest.NewRecorder()

			r.ServeHTTP(rec, req)

			if rec.Code != tt.wantStatus {
				t.Fatalf("status = %d, want %d (body: %s)", rec.Code, tt.wantStatus, rec.Body)
			}
			if tt.wantCode != "" {
				var body struct{ Code string }
				_ = json.Unmarshal(rec.Body.Bytes(), &body)
				if body.Code != tt.wantCode {
					t.Errorf("code = %q, want %q", body.Code, tt.wantCode)
				}
			}
		})
	}
}
```

The third case is what justifies this layer: it proves the domain error became a
409 through the error-mapping middleware. No unit test covers that — the service
only knows how to return `ErrAlreadyPaid`.

Include the body in the failure message. A bare `status = 500, want 200` sends you
to the logs; with the body you usually see the cause immediately.

## Repository tests against real Postgres

sqlc generates SQL against a real schema, so the thing that can be wrong is the
SQL — and only a real database can tell you. Never substitute SQLite: it silently
lacks `JSONB`, `ON CONFLICT` behavior, array types and the transaction semantics
you depend on.

```go
// internal/order/store_test.go
func TestMain(m *testing.M) {
	ctx := context.Background()

	pg, err := postgres.Run(ctx, "postgres:16-alpine",
		postgres.WithDatabase("test"),
		testcontainers.WithWaitStrategy(
			wait.ForLog("database system is ready to accept connections").
				WithOccurrence(2).WithStartupTimeout(30*time.Second)),
	)
	if err != nil {
		log.Fatalf("start postgres: %v", err)
	}

	dsn, _ := pg.ConnectionString(ctx, "sslmode=disable")
	if err := goose.Up(sqlOpen(dsn), "../../db/migrations"); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	testPool, _ = pgxpool.New(ctx, dsn)

	code := m.Run()

	_ = pg.Terminate(ctx)
	os.Exit(code)
}
```

`TestMain` starts the container **once per package**, which is what keeps this fast
enough to run on every commit. Running migrations against it — rather than
hand-writing a schema for tests — is what proves the migrations themselves work.

Isolate each test with a transaction that is rolled back:

```go
func withTx(t *testing.T) *Store {
	t.Helper()
	tx, err := testPool.Begin(context.Background())
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	t.Cleanup(func() { _ = tx.Rollback(context.Background()) })
	return NewStore(tx)
}

func TestStore_GetOrder(t *testing.T) {
	store := withTx(t)
	ctx := context.Background()

	seed(t, store, Order{ID: "o1", CustomerID: "c1", AmountCents: 1000})

	got, err := store.GetOrder(ctx, "o1")

	if err != nil {
		t.Fatalf("GetOrder() error = %v", err)
	}
	if got.AmountCents != 1000 {
		t.Errorf("AmountCents = %d, want 1000", got.AmountCents)
	}
}

func TestStore_GetOrder_NotFound(t *testing.T) {
	_, err := withTx(t).GetOrder(context.Background(), "nope")

	if !errors.Is(err, ErrNotFound) {
		t.Fatalf("error = %v, want ErrNotFound", err)
	}
}
```

`t.Cleanup` runs even when the test fails, so a failure never leaves rows behind for
the next test. Rolling back beats truncating: no schema recreation, and no residue
from a test that panicked halfway.

The second test is the one people skip, and it is the one that catches a real bug —
that `pgx.ErrNoRows` is translated into a domain error at the boundary rather than
leaking upward.

Always test a round trip — write then read — so the mapping is exercised in both
directions. A conversion bug that only exists on read survives a write-only test.

## What not to test

- `binding:"required"` tags — that is the framework
- Getters with no logic
- sqlc-generated code itself
- Anything where the test cannot fail because of a bug of yours

## Useful tooling

```bash
go test ./...                       # all packages, cached
go test -race ./...                 # data races — see go-concurrency
go test -run TestOrder_MarkPaid ./internal/order
go test -count=1 ./...              # bypass the cache
go test -cover ./...
go test -coverprofile=c.out ./... && go tool cover -html=c.out
```

`go test` caches results and skips packages whose inputs have not changed, which is
why the full suite is usually near-instant. `-count=1` is the documented way to
force a rerun when you suspect a flake.

`t.Setenv` restores the previous value automatically and marks the test as unable to
run in parallel — which is correct, since environment variables are process-wide.

`testify` is worth adding for `require.NoError` and `assert.Equal` on large structs;
both read well. It does not replace the table-driven structure, and `require` (which
stops the test) versus `assert` (which continues) is the same distinction as
`Fatalf` versus `Errorf`.

## Checklist

- Table-driven with `t.Run` and descriptive names
- `errors.Is` for error assertions, never `==`
- Hand-written fakes that record what they received
- Handler tests cover at least one success and one domain-error status
- Repository tests run against real Postgres, migrated, with rollback per test
- The not-found path is tested, proving the error translation
- `t.Cleanup` rather than manual teardown
- `go test -race ./...` in CI
- No test depends on execution order, the clock, or the network
