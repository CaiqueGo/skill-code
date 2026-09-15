<!--
Template. Copy to the ROOT of a project that uses these skills:

    cp templates/CLAUDE.md /path/to/your-project/CLAUDE.md

Then delete the sections that do not apply (Terraform, for instance) and fill in
the Project facts section. Keep it short: this file is loaded into context on
every single session, so every line here costs tokens on every request. Anything
that is detail rather than instruction belongs in a skill, not here.

Why this file exists: skill descriptions only make a skill *available* — the model
still decides whether to consult it, and for a task it believes it can handle
alone ("create a GET endpoint"), it often does not. This file does not ask. It is
loaded unconditionally, so it turns the standards from a suggestion into a rule.
-->

# Project conventions

## Before writing code

Run the `project-context-discovery` skill at the start of a session, before
creating or changing anything. It reads how this project actually does things.
Where this project diverges from the standards in the skills, **this project
wins** for naming and organization — the skills win for correctness and security.

## Which skill applies

Consult these rather than working from memory. They exist because the memory
version drifts.

Applies in any stack:

| Working on | Use |
|---|---|
| check-then-act on shared state, money, stock, queue handlers, retries | `concurrency-correctness` |
| auth, secrets, user input reaching a query, new dependency | `code-security` |
| reviewing a diff or PR, including your own before opening it | `pr-review` |
| branches, commit messages, releases, reverts | `gitflow` |
| Makefile, docker compose, Dockerfile, "how do I run this" | `dev-environment` |
| any `.tf` file | `terraform-standards` |
| anything under `.github/workflows/` | `cicd-pipelines` |

<!-- Keep the block for this project's stack and delete the other. -->

**Python / FastAPI:**

| Working on | Use |
|---|---|
| any endpoint, module, or "where does this go" question | `fastapi-architecture` |
| anything with `async`/`await`, or a slowness or hang symptom | `fastapi-async` |
| writing or fixing any test | `python-testing` |

**Go / gin / sqlc:**

| Working on | Use |
|---|---|
| packages, handlers, sqlc queries, "where does this go", import cycles | `go-architecture` |
| goroutines, channels, `context`, races, leaks, shutdown | `go-concurrency` |
| writing or fixing any test | `go-testing` |

More than one usually applies. A new endpoint that reads from the database is the
architecture skill **and** `code-security`, and it is not finished without the
testing one.

## Non-negotiable

These hold regardless of what the surrounding code does. Finding an existing
violation is a reason to flag it, not a reason to copy it.

- Business rules never import the web framework or the database driver
  (`fastapi`/`sqlalchemy`, `gin`/`database/sql`)
- Database queries are parameterized — never an f-string, never concatenation,
  not even with a value that "comes from our own system"
- Ownership is a parameter of the query, not a check afterwards. An endpoint
  returning someone's data takes the owner id as an argument
- Explicit output schemas. Never return an ORM model or a free-form `dict`
- No secret in code, in a default value, in a log, or in an error message
- Every external call has an explicit timeout
- Reading state and then writing it back needs a constraint, a lock, or a single
  atomic statement — never a check in application code alone
- No external HTTP call inside a database transaction
- New behavior ships with a test that fails when the behavior is reverted

## Definition of done

A change is not done until: the test for the error path exists, not just the happy
path; nothing sensitive is logged or returned; and the diff has been read the way
a reviewer would read it (`git diff main...HEAD`).

## Project facts

<!-- Fill this in. It saves a discovery pass on every session. -->

- **Stack**:
- **Run tests**: `make test`
- **Run lint**: `make lint`
- **Start dependencies**: `make up`
- **Business layer is called**: `Manager` | `Service` | other
- **Migrations**:
- **Deploy**:

If those commands are not make targets yet, see `dev-environment` — a command that
only lives in a README drifts from reality within a month.
