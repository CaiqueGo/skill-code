---
name: go-concurrency
description: Goroutines, channels, context and shared memory in Go services — data races, goroutine leaks, cancellation and deadlines, errgroup and bounded parallelism, sync primitives, worker pools and graceful shutdown. Use ALWAYS when `go `, `chan`, `select`, `sync.`, `context.` or `errgroup` appears in the code, when a handler starts background work, and when the symptom points at concurrency inside the process — "it works locally but hangs in production", "memory keeps growing", "the race detector found something", "goroutine leak", "all goroutines are asleep - deadlock", flaky tests that pass alone, or requests that never return. This skill covers concurrency inside one process; wrong results from two requests hitting the same database row belong to `concurrency-correctness`.
---

# Concurrency in Go

## Scope

Go has real shared-memory concurrency: goroutines run in parallel on multiple
cores, and any value reachable from two of them is a potential data race. That is a
different world from Python, where the GIL and a single event loop remove most of
this — and it means the failures here are unique to Go.

This skill is about what happens **inside one process**. Two requests producing a
double charge, a lost update or a database deadlock is a different problem with
different fixes: see `concurrency-correctness` in `engineering-base`.

## Start with the race detector

Before reasoning about any of this, turn on the tool that finds it mechanically:

```bash
go test -race ./...
go build -race -o bin/api ./cmd/api     # a staging build, not production
```

The race detector reports the two stacks that touched the same address without
synchronization. It has almost no false positives — if it fires, there is a bug,
even if the code "works". It only sees races that actually execute, so it is only
as good as your test coverage of concurrent paths.

It costs roughly 5–10x CPU and memory, which is why it belongs in CI and staging
rather than production. `go test -race ./...` in CI is the single highest-value
line in a Go pipeline.

## Every goroutine needs an owner and an exit

A goroutine that never returns is a leak: its stack, and everything it references,
stays alive for the life of the process. Leaks are gradual, so they present as
memory growing over days and then an OOM kill — long after the deploy that caused
it.

```go
// LEAK: nobody cancels this, and if nobody reads ch it blocks forever
go func() {
    result := expensive()
    ch <- result
}()
```

Before writing `go`, answer two questions: **who waits for it**, and **what makes it
stop**. If either has no answer, it is a leak.

```go
// owned: the caller waits, and ctx makes it stop
g, ctx := errgroup.WithContext(ctx)
g.Go(func() error {
    return refreshCache(ctx)
})
if err := g.Wait(); err != nil {
    return err
}
```

**In a gin handler, `c.Request.Context()` is cancelled when the response is
written.** So background work started from a handler and given that context is
cancelled the moment you reply — usually not what was intended:

```go
func (h *Handler) create(c *gin.Context) {
    order, _ := h.service.Create(c.Request.Context(), req)

    // WRONG: cancelled as soon as this handler returns
    go h.notify(c.Request.Context(), order)

    // WRONG: never stops, not tracked, dies silently on deploy
    go h.notify(context.Background(), order)

    c.JSON(http.StatusOK, order)
}
```

Both are wrong for the same underlying reason: work that must actually happen does
not belong to a request that is already over. Put it in a durable queue, or hand it
to a component owned by `main` with its own lifecycle and shutdown:

```go
h.notifier.Enqueue(order)      // buffered channel, drained by a worker main owns
```

If the work may be lost, that is fine — say so explicitly, derive a context from
the application's lifetime rather than the request's, and track the goroutine so
shutdown can wait for it.

## Context: cancellation, deadlines, and what it is not

```go
ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
defer cancel()                       // always, even when the timeout fires

resp, err := s.client.Do(req.WithContext(ctx))
```

`defer cancel()` is not optional even when you expect the timeout to expire: the
timer and its parent registration live until `cancel` is called, so skipping it
leaks memory in a hot path. `go vet` catches the common form — the `lostcancel`
check is another reason to run it in CI.

Rules that matter:

- **First parameter, always**, named `ctx`. Never a struct field.
- **Do not pass `nil`** — use `context.Background()` at the top and
  `context.TODO()` only as a temporary marker.
- **`context.Value` is for request-scoped metadata** — request id, trace id, caller
  identity. Not for passing dependencies. A dependency in the context is an
  untyped, invisible parameter that fails at runtime.
- **Check `ctx.Err()` in long loops.** Cancellation is cooperative; nothing
  interrupts a goroutine that never looks.

```go
for _, item := range items {
    if err := ctx.Err(); err != nil {
        return err                   // client gave up; stop working
    }
    process(ctx, item)
}
```

## Parallel calls with errgroup

```go
func (s *Service) Dashboard(ctx context.Context, userID string) (Dashboard, error) {
    var (
        profile Profile
        orders  []Order
    )

    g, ctx := errgroup.WithContext(ctx)
    g.Go(func() (err error) {
        profile, err = s.profiles.Get(ctx, userID)
        return err
    })
    g.Go(func() (err error) {
        orders, err = s.orders.ListByCustomer(ctx, userID, 20)
        return err
    })

    if err := g.Wait(); err != nil {
        return Dashboard{}, err
    }
    return Dashboard{Profile: profile, Orders: orders}, nil
}
```

`errgroup.WithContext` returns a context cancelled as soon as any goroutine fails,
so the siblings stop instead of finishing work whose result is discarded. Reusing
the name `ctx` for the derived one is deliberate: it prevents accidentally passing
the uncancelled parent into a child.

Writing to distinct variables from separate goroutines is safe — different memory,
and `g.Wait()` establishes the happens-before edge before you read them. Appending
to one shared slice from several goroutines is a data race.

**Bound the parallelism** whenever the count comes from data:

```go
g.SetLimit(10)                       // at most 10 in flight
for _, id := range ids {             // ids may be 5 or 50,000
    g.Go(func() error { return s.fetch(ctx, id) })
}
```

Without a limit, 50,000 ids means 50,000 concurrent requests: the connection pool
saturates, the downstream service is overwhelmed, and the failure usually lands on
them before it lands on you.

Note that since Go 1.22 the loop variable is per-iteration, so capturing `id`
directly is safe. On older versions this same loop silently sends the last id every
time — a classic bug worth recognizing in existing code.

## Sharing memory

The guidance is real but often misquoted: *share memory by communicating*. When
sharing is genuinely simpler, use a mutex and keep it small.

```go
type Cache struct {
    mu     sync.RWMutex
    values map[string]Entry
}

func (c *Cache) Get(k string) (Entry, bool) {
    c.mu.RLock()
    defer c.mu.RUnlock()
    v, ok := c.values[k]
    return v, ok
}

func (c *Cache) Set(k string, v Entry) {
    c.mu.Lock()
    defer c.mu.Unlock()
    c.values[k] = v
}
```

- **The mutex sits next to the data it protects**, unexported, and callers never
  see it. A mutex in one place guarding data that is also read elsewhere protects
  nothing.
- **Never copy a struct containing a `sync.Mutex`.** The copy has its own lock and
  both are useless. `go vet` catches this; it is why such types are used through
  pointers.
- **`RWMutex` only pays off with many readers and rare writers.** Under mixed load
  it is slower than `Mutex` because of the bookkeeping.
- **Maps are not safe for concurrent use**, and concurrent map writes are a
  deliberate fatal panic — not a race the detector merely reports.
- **`sync.Once` for lazy initialization** rather than a nil check, which races.
- **`atomic.Int64` for a plain counter** — no lock needed for a single value.

## Channels

Use a channel when values move between goroutines, not as a substitute for a
mutex around shared state.

```go
// worker pool: fixed workers, bounded queue
jobs := make(chan Job, 100)

var wg sync.WaitGroup
for i := 0; i < 8; i++ {
    wg.Add(1)
    go func() {
        defer wg.Done()
        for job := range jobs {         // exits when jobs is closed
            process(ctx, job)
        }
    }()
}

for _, j := range list {
    jobs <- j
}
close(jobs)                             // the sender closes, always
wg.Wait()
```

The rules that prevent the usual deadlocks:

- **The sender closes the channel**, never a receiver. Sending on a closed channel
  panics.
- **`for range` over a channel ends when it is closed.** Forget the close and every
  worker blocks forever — the goroutines stay parked and the process leaks them.
- **An unbuffered send blocks until a receiver is ready.** With no receiver, that
  is a deadlock; if every goroutine is blocked, the runtime prints "all goroutines
  are asleep - deadlock!" and exits. That message means a whole-program deadlock, so
  in a server it usually points at a test or an init path rather than a handler.
- **`select` with `case <-ctx.Done()`** whenever a send or receive could block
  indefinitely.

```go
select {
case results <- value:
case <-ctx.Done():
    return ctx.Err()
}
```

Without the second case, a cancelled consumer leaves this goroutine blocked on the
send forever.

## Graceful shutdown

Shutdown is a concurrency problem, and getting it wrong shows up as a small,
permanent error rate around every deploy:

```go
ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
defer stop()

// ... start server in a goroutine ...

<-ctx.Done()

shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
defer cancel()
if err := srv.Shutdown(shutdownCtx); err != nil {
    slog.Error("graceful shutdown failed", "err", err)
}
workers.StopAndWait(shutdownCtx)     // drain your own goroutines too
```

Note the fresh `context.Background()` for the shutdown timeout: deriving it from the
already-cancelled `ctx` gives a context that is dead on arrival, and `Shutdown`
returns immediately without draining anything.

Keep the timeout below the orchestrator's grace period, or SIGKILL arrives
mid-drain and you gained nothing.

## Diagnosis

```bash
go test -race ./...                            # data races
go vet ./...                                   # lostcancel, copylocks, loopclosure
kill -QUIT <pid>                               # dump every goroutine stack
curl localhost:6060/debug/pprof/goroutine?debug=1   # with net/http/pprof
```

The goroutine profile is the fastest way to confirm a leak: take it twice, minutes
apart, and compare counts. A group growing steadily with the same stack is the
leak, and the stack names the line that created it.

"all goroutines are asleep - deadlock!" means the runtime found that nothing can
ever proceed. Read the stacks it prints: each one shows what it is blocked on, and
the cycle is usually obvious once they are side by side.

## Checklist

- `go test -race ./...` runs in CI
- Every `go` statement has a known owner and a way to stop
- No goroutine started from a handler using the request context for work that must outlive it
- `defer cancel()` after every `WithTimeout`/`WithCancel`
- `errgroup.SetLimit` whenever the count comes from data
- Mutex unexported, next to its data, never copied
- The sender closes the channel; every `range` over a channel can terminate
- `select` includes `ctx.Done()` wherever a block is possible
- Graceful shutdown uses a fresh context, and drains workers as well as the server
