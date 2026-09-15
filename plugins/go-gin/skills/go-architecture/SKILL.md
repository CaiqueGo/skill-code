---
name: go-architecture
description: Structure for Go HTTP services built on gin and sqlc — package per domain, where each rule belongs, interfaces declared where they are consumed, error handling with wrapping and sentinel values, context propagation, and how sqlc-generated code fits as the data layer. Use ALWAYS when creating, reviewing or refactoring a Go service: new endpoint, new domain package, "where do I put this business rule", gin handler, sqlc query, `Querier`, repository, service, dependency wiring in main, import cycle errors, or when a handler starts growing. Trigger even when the user never says "architecture" — "how should I organize this Go service", "this handler is doing too much", "where does this validation go", or an import cycle compile error are the primary cases.
---

# Go service architecture — gin and sqlc

## The same rule, enforced by the compiler

The rule is the one from any layered design: **business logic must not know that
HTTP exists, and must not know how data is stored.** What is different in Go is
that you get a compiler check for free — an import cycle is a build error, not a
lint warning. Structure the packages so that violating the layering cannot compile.

That changes the shape. In Python the layers are directories and a linter enforces
them. In Go the unit is the **package**, and the natural grouping is per domain:

```
cmd/api/main.go             composition root — the only place that knows everything
internal/
  order/                    one package per domain
    order.go                entity, invariants, sentinel errors
    service.go              use cases; imports nothing from gin or database/sql
    handler.go              gin handlers; imports service
    store.go                adapts sqlc-generated code to what service needs
  customer/
    ...
  platform/
    postgres/               pool setup, transaction helper
    httpx/                  shared middleware, error mapping
db/
  migrations/               goose or golang-migrate
  query/                    the .sql files sqlc reads
  sqlc/                     GENERATED — never edited by hand
```

`internal/` is not decoration: packages under it cannot be imported from outside
the module, so a service's internals cannot leak into someone else's code.

Grouping per domain rather than per layer (`internal/handlers/`,
`internal/services/`) matters in Go specifically: layer packages produce import
cycles constantly, because `handlers` needs `services` and shared types end up
needed by both. A domain package has one inward direction and never cycles.

## Interfaces are declared by the consumer

This is the Go idiom that most changes the design, and the one people coming from
Python or Java get backwards. **The consumer declares the interface it needs; the
implementation does not export one.**

```go
// internal/order/service.go — the service says what it needs, narrowly
type Store interface {
    GetOrder(ctx context.Context, id string) (Order, error)
    MarkPaid(ctx context.Context, arg MarkPaidParams) error
}

type PaymentGateway interface {
    Charge(ctx context.Context, customerID string, amountCents int64) (Receipt, error)
}

type Service struct {
    store    Store
    payments PaymentGateway
}

func NewService(store Store, payments PaymentGateway) *Service {
    return &Service{store: store, payments: payments}
}
```

The store package exports a concrete `*Store` struct and never mentions this
interface. Go satisfies it structurally, so nothing needs to be wired up.

Why it matters: the interface lists exactly the two methods this service uses,
not the forty that sqlc generated. A fake in a test implements two methods. And
when the service later needs a third, the interface change sits next to the code
that needs it — where you are already reading.

**Accept interfaces, return structs.** `NewService` takes interfaces and returns
`*Service`, so callers get the concrete type with all its methods.

## Where to put the rule

- **Invariant of one entity?** (a status only moves from A to B) → a method on the
  type in `order.go`.
- **Coordinates several things or several I/O calls?** → `service.go`.
- **Request shape?** (required field, valid email) → the binding struct in
  `handler.go`.
- **How data is stored?** (join, upsert, index) → the `.sql` file, then `store.go`.

Format validation on the binding struct, business rules in the domain. A
`binding:"required,email"` tag is request shape; "a blocked customer cannot
purchase" never is.

## The entity guards itself

```go
// internal/order/order.go
type Status string

const (
    StatusPending Status = "pending"
    StatusPaid    Status = "paid"
)

type Order struct {
    ID          string
    CustomerID  string
    AmountCents int64
    Status      Status
    ReceiptID   *string
    PaidAt      *time.Time
}

func (o *Order) MarkPaid(receiptID string, now time.Time) error {
    if o.Status == StatusPaid {
        return fmt.Errorf("order %s: %w", o.ID, ErrAlreadyPaid)
    }
    o.Status = StatusPaid
    o.ReceiptID = &receiptID
    o.PaidAt = &now
    return nil
}

func (o *Order) AssertAmount(amountCents int64) error {
    if amountCents != o.AmountCents {
        return fmt.Errorf("expected %d got %d: %w", o.AmountCents, amountCents, ErrAmountMismatch)
    }
    return nil
}
```

A struct with only exported fields and no methods is an anemic model: the same
rule then gets rewritten in every caller, and they drift.

## Errors: sentinels plus wrapping

Go has no exception hierarchy, so the equivalent is a set of sentinel errors that
callers match with `errors.Is`:

```go
// internal/order/order.go
var (
    ErrNotFound       = errors.New("order not found")
    ErrAlreadyPaid    = errors.New("order already paid")
    ErrAmountMismatch = errors.New("amount does not match order")
)
```

Wrap with `%w` when adding context, so the sentinel survives:

```go
order, err := s.store.GetOrder(ctx, id)
if err != nil {
    return Order{}, fmt.Errorf("get order %s: %w", id, err)   // %w, not %v
}
```

`%v` flattens the error into a string and `errors.Is` stops working two layers up —
a silent break, because the code still compiles and the message still looks right.

Add context that the caller does not already have. `fmt.Errorf("get order: %w", err)`
inside `GetOrder` is noise; the identifier that failed is not.

## The handler stays thin

```go
// internal/order/handler.go
type Handler struct{ service *Service }

func NewHandler(s *Service) *Handler { return &Handler{service: s} }

func (h *Handler) Register(r gin.IRouter) {
    g := r.Group("/orders")
    g.GET("/:id", h.get)
    g.POST("/:id/pay", h.pay)
}

type payRequest struct {
    AmountCents int64 `json:"amount_cents" binding:"required,gt=0"`
}

func (h *Handler) pay(c *gin.Context) {
    var req payRequest
    if err := c.ShouldBindJSON(&req); err != nil {
        httpx.Fail(c, http.StatusBadRequest, "invalid_body", err.Error())
        return
    }

    order, err := h.service.Pay(c.Request.Context(), c.Param("id"), req.AmountCents)
    if err != nil {
        httpx.Error(c, err)       // one place maps domain errors to status codes
        return
    }

    c.JSON(http.StatusOK, toResponse(order))
}
```

Three details that are easy to get wrong:

- **`c.Request.Context()`, never `c` itself**, as the context passed inward. The
  request context is cancelled when the client disconnects, which is what lets a
  slow query be abandoned instead of finishing for nobody. `*gin.Context`
  implements `context.Context`, so passing it compiles and silently loses that.
- **`ShouldBindJSON`, not `BindJSON`.** The `Bind*` family writes a 400 and aborts
  on its own, so your error shape is whatever gin decided. `ShouldBind*` returns
  the error and lets you produce the same body as every other failure.
- **The response struct is explicit.** Returning the entity means a new internal
  field appears in the public API the day someone adds it.

## Mapping domain errors to status codes, once

```go
// internal/platform/httpx/errors.go
func Error(c *gin.Context, err error) {
    switch {
    case errors.Is(err, order.ErrNotFound):
        Fail(c, http.StatusNotFound, "order_not_found", "order not found")
    case errors.Is(err, order.ErrAlreadyPaid):
        Fail(c, http.StatusConflict, "order_already_paid", "order already paid")
    case errors.Is(err, order.ErrAmountMismatch):
        Fail(c, http.StatusUnprocessableEntity, "amount_mismatch", "amount does not match")
    default:
        slog.ErrorContext(c.Request.Context(), "unhandled error", "err", err)
        Fail(c, http.StatusInternalServerError, "internal_error", "internal error")
    }
}
```

The default branch logs the real error and returns a generic message: error text
frequently contains table names, query fragments or connection strings, and none of
that belongs in a response.

As domains multiply, let each expose its own mapping rather than growing one giant
switch that imports every package.

## sqlc as the data layer

sqlc generates Go from your SQL — you write queries, it writes the types. The
generated code is the *driver*, not your repository:

```go
// internal/order/store.go
type Store struct{ q *sqlc.Queries }

func NewStore(db sqlc.DBTX) *Store { return &Store{q: sqlc.New(db)} }

func (s *Store) GetOrder(ctx context.Context, id string) (Order, error) {
    row, err := s.q.GetOrder(ctx, id)
    if errors.Is(err, sql.ErrNoRows) {
        return Order{}, ErrNotFound          // translate at the boundary
    }
    if err != nil {
        return Order{}, fmt.Errorf("query order %s: %w", id, err)
    }
    return toDomain(row), nil
}
```

Two boundaries are being crossed here and both matter:

1. **`sql.ErrNoRows` becomes a domain error.** Otherwise every caller imports
   `database/sql` to check it, and the storage detail has leaked all the way up.
2. **The generated row becomes the domain type.** sqlc rows carry
   `pgtype.Timestamptz` and `sql.NullString`; those belong to the driver, not to
   your business logic.

Never edit generated files, and never let `sqlc.Queries` reach the service — the
service depends on the narrow interface it declared, which `*Store` satisfies.

Full sqlc mechanics — config, nullability, transactions, `:copyfrom`, migrations —
are in `references/sqlc.md`. Read it when writing queries or changing the schema.

## Wiring in main

```go
// cmd/api/main.go
func main() {
    cfg := config.Load()
    pool, err := pgxpool.New(ctx, cfg.DatabaseURL)
    // ...
    orderSvc := order.NewService(order.NewStore(pool), stripe.New(cfg.StripeKey))

    r := gin.New()
    r.Use(gin.Recovery(), httpx.RequestID(), httpx.Logger())
    order.NewHandler(orderSvc).Register(r.Group("/v1"))

    srv := &http.Server{
        Addr:              cfg.Addr,
        Handler:           r,
        ReadHeaderTimeout: 5 * time.Second,
    }
    // graceful shutdown on SIGTERM, see references/complete-module-example.md
}
```

`main` is the only place that knows every package. Nothing constructs its own
dependencies — that is what makes them replaceable in a test.

`gin.New()` and not `gin.Default()`: `Default` installs gin's own logger, which
prints unstructured lines that will not match the rest of your logging. Add
`Recovery` explicitly.

`ReadHeaderTimeout` is not optional — an `http.Server` with no timeouts will hold
connections open indefinitely, which is a trivial denial of service.

## Context

Every function that does I/O takes `ctx context.Context` as its **first**
parameter, and passes it down. It carries cancellation, deadlines and request
identity. A layer that drops it breaks cancellation for everything below.

Never store a context in a struct field. It is per-call, not per-object.

## Checklist

- No import of `gin` or `database/sql` inside `service.go` or the entity file
- Interfaces declared in the package that consumes them, listing only what is used
- `c.Request.Context()` passed inward, never `*gin.Context`
- `ShouldBind*` rather than `Bind*`
- Errors wrapped with `%w`; sentinels matched with `errors.Is`
- `sql.ErrNoRows` translated at the store boundary
- Generated sqlc code never edited, never reaching the service
- Explicit response structs, never the entity
- `main` is the only package that constructs dependencies
- `http.Server` has timeouts configured

## References

- `references/sqlc.md` — configuration, nullability, transactions, `:copyfrom`,
  migration workflow and the regeneration loop. Read when writing a query or
  changing the schema.
- `references/complete-module-example.md` — an entire domain package, file by file,
  including tests and graceful shutdown. Read when creating a package from scratch.
- Goroutines, channels, `context` cancellation and data races: `go-concurrency`.
- Wrong results under concurrency — double charges, lost updates, deadlocks:
  `concurrency-correctness` in `engineering-base`.
