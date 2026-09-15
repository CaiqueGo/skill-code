# Complete domain package — `internal/order`

Every file of one domain, in the order it should be written. Writing inside-out
(entity first, handler last) keeps the JSON shape from leaking into the model.

## 1. `db/migrations/20260915093000_create_orders.sql`

```sql
-- +goose Up
CREATE TABLE orders (
    id           TEXT PRIMARY KEY,
    customer_id  TEXT   NOT NULL,
    amount_cents BIGINT NOT NULL CHECK (amount_cents > 0),
    status       TEXT   NOT NULL DEFAULT 'pending',
    receipt_id   TEXT,
    paid_at      TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_orders_customer ON orders (customer_id, created_at DESC);

-- +goose Down
DROP TABLE orders;
```

The index exists because `ListByCustomer` filters and sorts on exactly those
columns. An index added later, after the table has grown, needs
`CREATE INDEX CONCURRENTLY` and a maintenance window.

## 2. `db/query/order.sql`

```sql
-- name: GetOrder :one
SELECT id, customer_id, amount_cents, status, receipt_id, paid_at
FROM orders WHERE id = $1;

-- name: ListOrdersByCustomer :many
SELECT id, customer_id, amount_cents, status, receipt_id, paid_at
FROM orders WHERE customer_id = $1
ORDER BY created_at DESC LIMIT $2;

-- name: MarkOrderPaidIfPending :execrows
UPDATE orders
SET status = 'paid', receipt_id = $2, paid_at = $3
WHERE id = $1 AND status = 'pending';
```

`:execrows` on the conditional update: zero rows affected means it was already
paid, decided by the database in one statement rather than by a read-then-write in
Go.

## 3. `internal/order/order.go` — entity and sentinels

```go
package order

import (
	"errors"
	"fmt"
	"time"
)

var (
	ErrNotFound       = errors.New("order not found")
	ErrAlreadyPaid    = errors.New("order already paid")
	ErrAmountMismatch = errors.New("amount does not match order")
)

type Status string

const (
	StatusPending   Status = "pending"
	StatusPaid      Status = "paid"
	StatusCancelled Status = "cancelled"
)

type Order struct {
	ID          string
	CustomerID  string
	AmountCents int64
	Status      Status
	ReceiptID   *string
	PaidAt      *time.Time
}

func (o *Order) AssertAmount(amountCents int64) error {
	if amountCents != o.AmountCents {
		return fmt.Errorf("expected %d got %d: %w", o.AmountCents, amountCents, ErrAmountMismatch)
	}
	return nil
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
```

## 4. `internal/order/service.go`

```go
package order

import (
	"context"
	"fmt"
	"time"
)

// Declared here because this is where they are consumed — two methods, not forty.
type Store interface {
	GetOrder(ctx context.Context, id string) (Order, error)
	MarkPaidIfPending(ctx context.Context, id, receiptID string, paidAt time.Time) (int64, error)
}

type Receipt struct {
	ID          string
	AmountCents int64
}

type PaymentGateway interface {
	Charge(ctx context.Context, customerID string, amountCents int64, idempotencyKey string) (Receipt, error)
}

type Service struct {
	store    Store
	payments PaymentGateway
	now      func() time.Time      // injected so tests control the clock
}

func NewService(store Store, payments PaymentGateway) *Service {
	return &Service{store: store, payments: payments, now: time.Now}
}

func (s *Service) Get(ctx context.Context, id string) (Order, error) {
	o, err := s.store.GetOrder(ctx, id)
	if err != nil {
		return Order{}, fmt.Errorf("get order %s: %w", id, err)
	}
	return o, nil
}

func (s *Service) Pay(ctx context.Context, id string, amountCents int64) (Order, error) {
	o, err := s.Get(ctx, id)
	if err != nil {
		return Order{}, err
	}
	if err := o.AssertAmount(amountCents); err != nil {
		return Order{}, err
	}

	// outside any transaction; the key makes a retry safe
	receipt, err := s.payments.Charge(ctx, o.CustomerID, amountCents, "pay:"+o.ID)
	if err != nil {
		return Order{}, fmt.Errorf("charge order %s: %w", o.ID, err)
	}

	now := s.now()
	if err := o.MarkPaid(receipt.ID, now); err != nil {
		return Order{}, err
	}

	// conditional update: 0 rows means someone else paid it first
	affected, err := s.store.MarkPaidIfPending(ctx, o.ID, receipt.ID, now)
	if err != nil {
		return Order{}, fmt.Errorf("persist payment for %s: %w", o.ID, err)
	}
	if affected == 0 {
		return Order{}, fmt.Errorf("order %s: %w", o.ID, ErrAlreadyPaid)
	}

	return o, nil
}
```

No `gin`, no `database/sql`, no `pgx`. This compiles and runs identically from a
queue consumer or a CLI command.

## 5. `internal/order/store.go`

```go
package order

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"myapp/db/sqlc"
)

type Store struct{ q *sqlc.Queries }

func NewStore(db sqlc.DBTX) *Store { return &Store{q: sqlc.New(db)} }

func (s *Store) GetOrder(ctx context.Context, id string) (Order, error) {
	row, err := s.q.GetOrder(ctx, id)
	if errors.Is(err, pgx.ErrNoRows) {
		return Order{}, ErrNotFound          // storage detail stops here
	}
	if err != nil {
		return Order{}, fmt.Errorf("query order %s: %w", id, err)
	}
	return toDomain(row), nil
}

func (s *Store) MarkPaidIfPending(
	ctx context.Context, id, receiptID string, paidAt time.Time,
) (int64, error) {
	return s.q.MarkOrderPaidIfPending(ctx, sqlc.MarkOrderPaidIfPendingParams{
		ID: id, ReceiptID: &receiptID, PaidAt: &paidAt,
	})
}

func toDomain(r sqlc.Order) Order {
	return Order{
		ID:          r.ID,
		CustomerID:  r.CustomerID,
		AmountCents: r.AmountCents,
		Status:      Status(r.Status),
		ReceiptID:   r.ReceiptID,
		PaidAt:      r.PaidAt,
	}
}
```

`*Store` satisfies the `Store` interface structurally — it does not declare that it
does, and nothing needs to register it.

## 6. `internal/order/handler.go`

```go
package order

import (
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"myapp/internal/platform/httpx"
)

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

type response struct {
	ID          string     `json:"id"`
	CustomerID  string     `json:"customer_id"`
	AmountCents int64      `json:"amount_cents"`
	Status      Status     `json:"status"`
	PaidAt      *time.Time `json:"paid_at,omitempty"`
}

func toResponse(o Order) response {
	return response{
		ID: o.ID, CustomerID: o.CustomerID,
		AmountCents: o.AmountCents, Status: o.Status, PaidAt: o.PaidAt,
	}
}

func (h *Handler) get(c *gin.Context) {
	o, err := h.service.Get(c.Request.Context(), c.Param("id"))
	if err != nil {
		httpx.Error(c, err)
		return
	}
	c.JSON(http.StatusOK, toResponse(o))
}

func (h *Handler) pay(c *gin.Context) {
	var req payRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		httpx.Fail(c, http.StatusBadRequest, "invalid_body", err.Error())
		return
	}

	o, err := h.service.Pay(c.Request.Context(), c.Param("id"), req.AmountCents)
	if err != nil {
		httpx.Error(c, err)
		return
	}
	c.JSON(http.StatusOK, toResponse(o))
}
```

`response` exists separately from `Order` so a new internal field does not appear
in the public contract by accident.

## 7. `internal/platform/httpx/errors.go`

```go
package httpx

import (
	"errors"
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"
	"myapp/internal/order"
)

type errorBody struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func Fail(c *gin.Context, status int, code, message string) {
	c.AbortWithStatusJSON(status, errorBody{Code: code, Message: message})
}

func Error(c *gin.Context, err error) {
	switch {
	case errors.Is(err, order.ErrNotFound):
		Fail(c, http.StatusNotFound, "order_not_found", "order not found")
	case errors.Is(err, order.ErrAlreadyPaid):
		Fail(c, http.StatusConflict, "order_already_paid", "order already paid")
	case errors.Is(err, order.ErrAmountMismatch):
		Fail(c, http.StatusUnprocessableEntity, "amount_mismatch", "amount does not match order")
	default:
		slog.ErrorContext(c.Request.Context(), "unhandled error",
			"err", err, "path", c.FullPath())
		Fail(c, http.StatusInternalServerError, "internal_error", "internal error")
	}
}
```

The default branch logs the real error and returns a generic message: raw error text
often contains table names and query fragments.

## 8. `internal/order/service_test.go`

```go
package order

import (
	"context"
	"errors"
	"testing"
	"time"
)

type fakeStore struct {
	order    Order
	getErr   error
	affected int64
	marked   bool
}

func (f *fakeStore) GetOrder(_ context.Context, _ string) (Order, error) {
	return f.order, f.getErr
}

func (f *fakeStore) MarkPaidIfPending(_ context.Context, _, _ string, _ time.Time) (int64, error) {
	f.marked = true
	return f.affected, nil
}

type fakeGateway struct{ calls int }

func (f *fakeGateway) Charge(_ context.Context, _ string, cents int64, _ string) (Receipt, error) {
	f.calls++
	return Receipt{ID: "rc_1", AmountCents: cents}, nil
}

func TestPay(t *testing.T) {
	base := Order{ID: "o1", CustomerID: "c1", AmountCents: 1000, Status: StatusPending}

	tests := []struct {
		name       string
		amount     int64
		affected   int64
		wantErr    error
		wantCharge int
	}{
		{name: "pays and persists", amount: 1000, affected: 1, wantCharge: 1},
		{name: "amount mismatch does not charge", amount: 999, wantErr: ErrAmountMismatch},
		{name: "lost race reports conflict", amount: 1000, affected: 0,
			wantErr: ErrAlreadyPaid, wantCharge: 1},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			store := &fakeStore{order: base, affected: tt.affected}
			gateway := &fakeGateway{}
			svc := NewService(store, gateway)

			_, err := svc.Pay(context.Background(), "o1", tt.amount)

			if !errors.Is(err, tt.wantErr) {
				t.Fatalf("err = %v, want %v", err, tt.wantErr)
			}
			if gateway.calls != tt.wantCharge {
				t.Errorf("charge calls = %d, want %d", gateway.calls, tt.wantCharge)
			}
		})
	}
}
```

The mismatch case asserts `gateway.calls == 0` — that the customer was **not**
charged. That is the assertion worth having, and it is only possible because the
fake records what it received. See `go-testing` for the full approach.

## 9. `cmd/api/main.go`

```go
func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	cfg := config.Load()

	pool, err := pgxpool.New(ctx, cfg.DatabaseURL)
	if err != nil {
		slog.Error("connect database", "err", err)
		os.Exit(1)
	}
	defer pool.Close()

	orderSvc := order.NewService(order.NewStore(pool), stripe.New(cfg.StripeKey))

	r := gin.New()
	r.Use(gin.Recovery(), httpx.RequestID(), httpx.Logger())
	r.GET("/health", func(c *gin.Context) { c.JSON(200, gin.H{"ok": true}) })
	order.NewHandler(orderSvc).Register(r.Group("/v1"))

	srv := &http.Server{
		Addr:              cfg.Addr,
		Handler:           r,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			slog.Error("server failed", "err", err)
			os.Exit(1)
		}
	}()
	slog.Info("listening", "addr", cfg.Addr)

	<-ctx.Done()        // SIGTERM from the orchestrator

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		slog.Error("graceful shutdown failed", "err", err)
	}
}
```

**Graceful shutdown is what makes a deploy invisible.** On SIGTERM, `Shutdown`
stops accepting new connections and lets in-flight requests finish. Without it,
every deploy drops whatever was mid-request — which shows up as a small, permanent
error rate that nobody can reproduce.

Keep the shutdown timeout below the orchestrator's grace period (ECS
`stopTimeout`, Kubernetes `terminationGracePeriodSeconds`), or you get SIGKILL in
the middle of draining anyway.
