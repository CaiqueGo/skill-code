---
name: fastapi-async
description: Concurrency, event loop and performance in asynchronous Python services (FastAPI, asyncio, httpx, SQLAlchemy async) — when to use `async def` versus `def`, what blocks the loop, parallelism with gather and TaskGroup, bounded concurrency, timeouts and background tasks. Use ALWAYS when `async`/`await`/`asyncio` appears in the code, when writing an endpoint that makes more than one external call, and especially when the symptom is about speed — "the API is slow", "it hangs under load", "the healthcheck fails in production but works locally", "requests are queueing up", intermittent timeouts, or unexplained CPU/latency. Trigger also on seeing `requests`, `time.sleep`, `open()` or a synchronous driver inside an `async` function. This skill is about throughput inside one process; wrong *results* under concurrency — double charges, lost updates, database deadlocks — belong to `concurrency-correctness` instead.
---

# Concurrency and the event loop in FastAPI

## Scope

This skill is about **throughput inside one process**: what keeps the event loop
moving and what stalls it. Wrong results when two requests touch the same state —
double charges, lost updates, database deadlocks, idempotency — are a different
problem with different fixes, and they live in `concurrency-correctness`.

The quick test: if the complaint is *slow*, you are in the right place. If the
complaint is *wrong*, you are not.

## The single rule

**A FastAPI process has one event loop, and it is a single thread.** Every
concurrent request in that process goes through it. While a coroutine does not
hand control back (`await`), nothing else in the process advances — not another
request, not the healthcheck.

Almost every FastAPI performance problem comes from this: the server is not slow, a
blocking call is holding the loop. And the symptom is deceptive, because in
development, with one request at a time, everything works.

## The decision that matters most: `async def` or `def`

FastAPI treats the two differently, and that is your safety net:

| You write | FastAPI does | Use when |
|---|---|---|
| `async def` | runs **on** the event loop | everything inside is an `await` on async I/O |
| `def` | runs **in** a threadpool | there is any blocking call |

The classic mistake is writing `async def` because "it is more modern" and calling
synchronous code inside. That is **worse** than `def`:

```python
# WORST of both worlds: blocks the entire loop
@router.get("/users")
async def list_users():
    return requests.get(URL).json()      # synchronous, on the loop

# CORRECT when the library is synchronous: FastAPI offloads it for you
@router.get("/users")
def list_users():
    return requests.get(URL).json()

# BEST: a genuinely async client
@router.get("/users")
async def list_users(client: httpx.AsyncClient = Depends(get_client)):
    response = await client.get(URL)
    return response.json()
```

When in doubt, `def` is the safe choice. `async def` is a promise that everything
inside is non-blocking, and whoever breaks that promise takes down the whole
process.

## What blocks the loop

Any of these inside `async def` is an incident waiting to happen:

| Blocking | Replacement |
|---|---|
| `requests.get(...)` | `await client.get(...)` (httpx) |
| `time.sleep(n)` | `await asyncio.sleep(n)` |
| `psycopg2`, `pymysql` | `asyncpg`, SQLAlchemy async |
| `redis.Redis` | `redis.asyncio.Redis` |
| `open(...).read()` | `await asyncio.to_thread(...)` or `aiofiles` |
| `boto3` | `aioboto3`, or `asyncio.to_thread` |
| `pandas`, parsing a large CSV | **process** pool |
| `bcrypt.hashpw`, `pbkdf2` | `asyncio.to_thread` |
| `subprocess.run` | `await asyncio.create_subprocess_exec` |

`ruff` catches much of this automatically with the `ASYNC` rules — enable
`select = [..., "ASYNC"]` in `pyproject.toml`. Cheaper than finding out in
production.

## When the synchronous library is unavoidable

Not everything has an async version. Push it to a thread:

```python
import asyncio

@router.post("/reports")
async def create_report(payload: ReportIn):
    # to_thread returns control to the loop while the thread works
    pdf = await asyncio.to_thread(render_pdf_sync, payload.data)
    return {"size": len(pdf)}
```

**Threads do not help CPU work** — the GIL makes them compete for the same core and
latency gets worse. Heavy CPU goes to a separate process:

```python
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.cpu_pool = ProcessPoolExecutor(max_workers=4)
    yield
    app.state.cpu_pool.shutdown(wait=True)

@router.post("/analyze")
async def analyze(payload: AnalyzeIn, request: Request):
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(request.app.state.cpu_pool, heavy_math, payload.data)
    return result
```

Practical rule: **waiting → thread; computing → process.** And if the computation
takes more than a few seconds, it does not belong in an HTTP endpoint at all — it
belongs in a queue.

## Parallelism: `gather` and `TaskGroup`

Two independent calls in sequence waste the sum of their latencies:

```python
# 300ms + 200ms = 500ms
user = await fetch_user(user_id)
orders = await fetch_orders(user_id)

# max(300ms, 200ms) = 300ms
user, orders = await asyncio.gather(fetch_user(user_id), fetch_orders(user_id))
```

`gather` only makes sense when the calls are **independent**. If the second needs
the first one's result, there is nothing to parallelize.

On Python 3.11+, prefer `TaskGroup` when one failure should cancel the rest:

```python
async with asyncio.TaskGroup() as tg:
    user_task = tg.create_task(fetch_user(user_id))
    orders_task = tg.create_task(fetch_orders(user_id))

user, orders = user_task.result(), orders_task.result()
```

The difference is failure behavior, and it matters: with `gather`, if one coroutine
fails the others **keep running** in the background — you get the exception but are
left with orphan tasks holding connections. `TaskGroup` cancels the siblings and
only then propagates. For an HTTP request, cancelling is almost always right: the
client is going to get an error anyway.

Use `gather(..., return_exceptions=True)` when partial success is acceptable — for
instance enriching a response with optional data from three services. Then handle
each result, because exceptions become values in the list:

```python
results = await asyncio.gather(*calls, return_exceptions=True)
ok = [r for r in results if not isinstance(r, BaseException)]
```

## Bounding concurrency

`gather` over 5000 items opens 5000 simultaneous connections. That does not go
faster: it saturates the client pool, blows the vendor's rate limit, and often takes
the downstream service down before it takes you down.

```python
async def fetch_all(ids: list[str], client: httpx.AsyncClient) -> list[dict]:
    semaphore = asyncio.Semaphore(20)      # explicit ceiling

    async def one(item_id: str) -> dict:
        async with semaphore:
            response = await client.get(f"/items/{item_id}")
            return response.json()

    return await asyncio.gather(*(one(i) for i in ids))
```

The right number comes from the downstream service (how many connections it
tolerates), not from preference. Keep it in `Settings` rather than hardcoded — it is
the first knob you turn during an incident.

## The background task that disappears

This bug is subtle and very common. `create_task` returns a weakly-held reference:
if nobody keeps the task, the garbage collector can kill it mid-flight.

```python
# WRONG: the task may vanish without running, with no error at all
asyncio.create_task(send_webhook(payload))

# RIGHT: hold a strong reference until it finishes
_background_tasks: set[asyncio.Task] = set()

def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
```

And understand the limit: FastAPI's `BackgroundTasks` and `create_task` run **in the
same process**. A deploy, crash or restart kills whatever was pending, with no retry
and no record. They are fine for work that may be lost (logs, metrics, cache
warming). They are not fine for confirmation emails, billing, or webhooks with
delivery guarantees — those need a durable queue (Celery, ARQ, SQS, RabbitMQ).

## Clients and sessions

**One `AsyncClient` per process**, created in `lifespan`. Creating one per request
throws away the connection pool and the TLS handshake — one of the most common
causes of high latency that "does not show up in the profiler":

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(5.0, connect=2.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    yield
    await app.state.http.aclose()


app = FastAPI(lifespan=lifespan)   # `@app.on_event("startup")` is deprecated
```

**Timeouts are not optional.** `httpx` without an explicit `timeout` waits forever;
one slow dependency turns into a pile-up of coroutines until the process dies. Every
external client gets a timeout, and it is smaller than your own server timeout.

**SQLAlchemy's `AsyncSession` is not safe for concurrent use.** One session per
request, and never the same session inside a `gather`:

```python
# WRONG: both coroutines use the same connection at the same time
await asyncio.gather(repo.get(a), repo.get(b))   # repo shares one session

# RIGHT: one session per concurrent task
async def load(item_id: str) -> Item:
    async with session_factory() as session:
        return await SqlItemRepository(session).get(item_id)

await asyncio.gather(load(a), load(b))
```

## Diagnosis

When the symptom is slowness under load, test the blocking hypothesis before
anything else — it is the most likely cause and the cheapest to check.

Enable asyncio debug mode: it logs a warning whenever a callback holds the loop for
more than 100ms.

```python
import asyncio
asyncio.get_event_loop().set_debug(True)     # or the PYTHONASYNCIODEBUG=1 env var
```

A field test that never lies: add a `/ping` endpoint that just returns
`{"ok": True}`. Under load, if `/ping` is also slow, the loop is blocked — the
problem is not the heavy endpoint itself, it is that it never yields. If `/ping`
stays instant, the bottleneck is I/O or the database, and the investigation goes
elsewhere.

## Checklist

- No synchronous I/O call inside `async def` (ruff `ASYNC` rules enabled)
- An endpoint using a synchronous library is `def`, not `async def`
- Every external HTTP client has an explicit timeout
- `AsyncClient` and the database engine are created in `lifespan`, not per request
- `gather`/`TaskGroup` only over independent calls, with a `Semaphore` when the list is variable
- `create_task` held in a strong reference
- Work that must not be lost is in a durable queue, not `BackgroundTasks`
- One `AsyncSession` per request, never shared across concurrent tasks
