---
name: code-security
description: Application and dependency security — injection, authentication and authorization, secrets, data exposure, and CVE scanning of libraries with pip-audit/osv-scanner. Use ALWAYS when code touches authentication, authorization, sessions, tokens, passwords, hashing, uploads, deserialization, SQL queries, `subprocess`/`eval` calls, CORS, or personal data. Use it also when adding or upgrading a dependency, when a Dependabot alert appears, and when the request is "is this secure?", "any vulnerability here?", "check the libraries", "audit the dependencies", "CVE scan", "does this version have a known flaw". Trigger before approving any PR that touches those areas.
---

# Code security

## Where problems actually show up

The distribution of API incidents is not uniform. In order of real frequency, not of
how interesting the attack is:

1. **Broken authorization** — the endpoint authenticates ("who are you") but does not
   authorize ("may you see *this* resource"). It is the #1 API flaw, and it is
   invisible to tests written with a single user.
2. **Leaked secret** — in code, in logs, in an error message, in git history.
3. **Vulnerable dependency** — a known CVE in a transitive library nobody chose to
   install.
4. **Injection** — SQL, command, template, path.
5. **Data exposure** — the endpoint returns more fields than it should.

## Object-level authorization — the most common flaw

```python
# VULNERABLE: authenticated, but anyone can read anyone else's order
@router.get("/orders/{order_id}")
async def get_order(order_id: str, user: User = Depends(current_user)):
    return await manager.get(OrderId(order_id))

# CORRECT: ownership is part of the query
@router.get("/orders/{order_id}")
async def get_order(order_id: str, user: User = Depends(current_user)):
    return await manager.get_for_customer(OrderId(order_id), user.customer_id)
```

The rule that eliminates the whole class: **the owner's identifier goes into the
query, not into a check afterwards.** `if order.customer_id != user.customer_id:
raise Forbidden` works, but it depends on someone remembering it in every new
endpoint — and one day nobody does. When ownership is a repository parameter,
forgetting becomes a type error.

Return **404, not 403**, for another owner's resource: a 403 confirms the resource
exists, which is already an information leak in an enumeration flow.

Test this explicitly. The test that catches this flaw needs **two** users:

```python
async def test_user_cannot_access_another_users_order(client, user_a, user_b) -> None:
    order = await create_order(owner=user_a)
    response = await client.get(f"/orders/{order.id}", headers=auth(user_b))
    assert response.status_code == 404
```

## Secrets

Never in code, not even as a default. They come from the environment, validated at
startup (Pydantic `Settings` — see `fastapi-architecture`). `api_key: str = "test"`
is literally how a test key reaches production.

**Secrets in logs and errors** are the quietest leak:

```python
# leaks the token to the client and to the log aggregator
raise HTTPException(500, f"failed calling {url}?token={token}")
logger.info("request", extra={"headers": dict(request.headers)})   # Authorization included
```

Never log: `Authorization`, `Cookie`, passwords, tokens, card PANs, full national
IDs, or the whole body of an authentication request. The error message returned to
the client is generic; the detail goes to the internal log with a `request_id` for
correlation.

**If a secret was committed, rotating the value is the only fix.** Removing it from
the code does not help — it remains in history, in forks and in clones. Rotate
first, clean history afterwards (knowing that cleaning rewrites hashes and breaks
existing clones).

Prevent it with `gitleaks` or `detect-secrets` in `pre-commit` — cheap, and it
catches the problem before it becomes one.

## Injection

```python
# SQL — never concatenate, never f-string, not even with a "trusted" value
await session.execute(text(f"SELECT * FROM orders WHERE id = '{order_id}'"))   # NO
await session.execute(text("SELECT * FROM orders WHERE id = :id"), {"id": order_id})

# Command — argument list, never shell=True with external input
subprocess.run(f"convert {filename} out.png", shell=True)          # NO
subprocess.run(["convert", filename, "out.png"], check=True, timeout=30)

# Path — resolve and confirm it stays inside the allowed root
target = (BASE / user_supplied_name).resolve()
if not target.is_relative_to(BASE.resolve()):
    raise ValidationError("invalid path")
```

Never `eval`, `exec`, `pickle.loads` or `yaml.load` on external input — use
`yaml.safe_load` and JSON. `pickle` of untrusted data is remote code execution, not
a "theoretical risk".

## Data exposure

Explicit output schemas, always — never return the entity or the ORM model
directly. A new domain field (`password_hash`, `internal_notes`, `risk_score`) leaks
the day someone adds it, and nobody notices because nothing broke.

```python
class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    # password_hash exists on the entity and simply is not here
```

Watch out for `response_model=None` and endpoints returning a free-form `dict`: both
disable this protection.

## Authentication

- Passwords with `argon2` (preferred) or `bcrypt`. Never MD5, SHA-1 or raw SHA-256 —
  they are too fast, and speed is exactly what a brute-force attack wants.
- Hashing is CPU-bound and blocks the event loop: `await asyncio.to_thread(hash, pwd)`.
- Compare tokens with `secrets.compare_digest`, not `==` — `==` returns early and
  leaks the length of the correct prefix through timing.
- Random tokens with `secrets.token_urlsafe`, never `random` (predictable).
- JWT: validate `exp`, `iss`, `aud` and **pin the algorithm**. Explicit
  `algorithms=["HS256"]`; accepting the algorithm from the header is the `alg: none`
  vulnerability.
- Identical login response for a nonexistent user and a wrong password, in both
  content and timing — the difference enumerates users.

## CORS and headers

```python
# `allow_origins=["*"]` with `allow_credentials=True` is invalid and dangerous
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,   # explicit list, from the environment
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
```

Rate limit authentication endpoints and anything expensive. Cap body and upload
size — without it, a 2GB POST is free denial of service.

## Vulnerable dependencies

This part needs tooling, not reading. **There is no reliable CVE MCP today** — the
path is CLI, which runs identically on your machine and in CI.

```bash
pip-audit                        # CVEs in the Python environment, PyPI Advisory + OSV
osv-scanner scan source -r .     # multi-ecosystem, reads lockfiles (Google/OSV)
```

Run both: `pip-audit` understands the resolved Python environment; `osv-scanner`
reads lockfiles from any ecosystem and covers the whole repository, including what
is not Python.

**The point nearly everyone gets wrong:** a scan that only reads the manifest
(`pyproject.toml`, `requirements.in`) sees what you declared, not what is installed.
The CVE is almost always in a **transitive** dependency — a library you never chose.
Scan the **lockfile** or the resolved environment, otherwise "the scan passed" is
false.

The triage flow for a finding is in `references/vulnerability-triage.md` — read it
when deciding whether a CVE is urgent, and before responding to a Dependabot alert.
Not every high-severity CVE affects you, and treating them all as urgent is how a
team learns to ignore the alert.

Per-ecosystem detail (commands, CI integration, lockfiles) in
`references/scanners-by-stack.md`.

## Tooling in CI

```yaml
- run: ruff check --select S .        # bandit built into ruff, nothing extra to install
- run: pip-audit --strict
- run: gitleaks detect --no-git       # secrets in the working tree
- run: semgrep --config=auto          # SAST, optional; good signal/noise on "auto"
```

`ruff --select S` covers the essentials of bandit without adding a tool to the
pipeline. `--strict` on `pip-audit` makes a vulnerability **fail** the build instead
of becoming a warning nobody reads.

## Checklist

- New endpoint authorizes per object, not just authenticates
- Authorization test with **two** different users
- No secret in code, logs, errors or history
- Parameterized queries; `subprocess` without `shell=True`
- Explicit output schema, no sensitive fields
- Passwords with argon2/bcrypt, off the event loop; tokens with `secrets`
- CORS with explicit origins; rate limit on auth
- `pip-audit` and `osv-scanner` green against the **lockfile**
- New dependency: is it maintained? what does it pull in?
