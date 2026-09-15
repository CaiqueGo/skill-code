---
name: dev-environment
description: How a project is run on a developer machine — the Makefile as the task interface, docker compose for local dependencies, and Dockerfiles for dev and production images. Use ALWAYS when creating or changing a `Makefile`, `docker-compose.yml`, `Dockerfile` or `.dockerignore`, when adding a command the team will run, and when the request is "how do I run this locally", "set up the project", "add a make target", "the new dev can't get it running", "our image is 1.2GB", "the container rebuilds everything on every change", hot reload, or a local database/redis/localstack for development. Trigger also when writing a README's setup section — if the steps are not a make target, they will drift.
---

# Development environment

## The problem it solves

Someone clones the repository on Monday. How long until they have it running, and
how do they know the commands?

The failure mode is not dramatic: the README says `uvicorn app.main:app --reload`,
someone changes the module path, the README is not updated, and the next person
loses forty minutes. Multiply by every command and every new hire.

The fix is that **the commands live in one executable place**, and the README points
at it rather than duplicating it. A `make test` that is wrong gets fixed the first
time it fails; a README that is wrong stays wrong for a year.

## Why make

Not because make is good at building software — it is the wrong tool for that — but
because it is a **uniform interface**. Every project in the organization answers to
`make test`, `make lint`, `make run` regardless of whether it is Python, Go or
Terraform. That is worth more than any individual feature, and it means CI and a
new developer run the same commands.

The alternatives are fine in isolation and lose that property: `npm scripts` only in
Node, `just` needs installing, `task` needs installing and a YAML file. Make is
already on every machine and in every CI image.

## Standard targets

The same names everywhere, so muscle memory transfers between repositories:

| Target | Does |
|---|---|
| `make help` | list targets — the **default** target |
| `make setup` | one-time: install dependencies, hooks, tools |
| `make up` / `make down` | start / stop local dependencies |
| `make run` | run the app against those dependencies |
| `make test` | fast tests |
| `make test-all` | including integration and e2e |
| `make lint` | linter and type checker |
| `make fmt` | format in place |
| `make check` | what CI runs — lint, types, tests |
| `make migrate` | apply migrations |
| `make clean` | remove artifacts, volumes, caches |

If a command is typed more than twice, it becomes a target. If a target is never
typed, delete it.

## The header worth copying

```make
.DEFAULT_GOAL := help
.PHONY: help setup up down run test test-all lint fmt check migrate clean

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
```

Four things there, each preventing a specific problem:

- **`.DEFAULT_GOAL := help`** — a bare `make` lists the targets instead of running
  whatever happens to be first. That single line is how the Makefile becomes
  self-documenting.
- **The `help` target** reads the `## ` comments, so the documentation cannot drift
  from the commands: it *is* the commands.
- **`.PHONY`** — without it, a target named `test` does nothing when a directory
  called `test/` exists, because make thinks the file is already up to date. This
  produces a genuinely baffling "make test does nothing" bug.
- **`.SHELLFLAGS := -eu -o pipefail -c`** — by default each recipe line runs in a
  shell without `-e`, so a failing command mid-recipe is ignored and the target
  reports success. With this, a broken step fails the build.

## A Makefile that covers both stacks

```make
# Python (uv)
setup: ## Install dependencies and git hooks
	uv sync --frozen
	uv run pre-commit install

test: ## Run fast tests
	uv run pytest tests/unit

test-all: up ## Run every test, including integration
	uv run pytest

lint: ## Lint and type-check
	uv run ruff check .
	uv run mypy src

fmt: ## Format in place
	uv run ruff format .
	uv run ruff check --fix .

run: up ## Run the API against local dependencies
	uv run uvicorn src.main:app --reload
```

```make
# Go
setup: ## Install tooling
	go mod download
	go install github.com/pressly/goose/v3/cmd/goose@latest
	go install github.com/sqlc-dev/sqlc/cmd/sqlc@latest

test: ## Run fast tests
	go test -race -short ./...

test-all: up migrate ## Run every test, including integration
	go test -race ./...

lint: ## Lint and vet
	golangci-lint run
	go vet ./...

fmt: ## Format in place
	gofmt -w .
	goimports -w .

generate: ## Regenerate sqlc after a schema or query change
	sqlc generate

run: up migrate ## Run the API against local dependencies
	go run ./cmd/api
```

Note `run: up` and `test-all: up` — a prerequisite, not a comment telling people to
remember. Nobody should be able to run the integration suite against nothing.

The Go `generate` target matters more than it looks: sqlc-generated code silently
describes a schema that no longer exists if you forget it (see `go-architecture`).

**`make check` is the contract with CI:**

```make
check: lint test-all ## Everything CI runs
```

CI then calls `make check` rather than listing the steps itself. That way "green
locally, red in CI" stops being possible for reasons of command drift, and changing
a step means changing one place.

## Local dependencies with compose

Compose runs what the application needs, not the application itself. Running the app
outside the container keeps the reload loop instant and the debugger attached.

```yaml
# docker-compose.yml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_PASSWORD: local
      POSTGRES_DB: app
    ports: ["5432:5432"]
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d app"]
      interval: 5s
      timeout: 3s
      retries: 10

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 10

volumes:
  pgdata:
```

```make
up: ## Start local dependencies and wait until healthy
	docker compose up -d --wait

down: ## Stop them, keeping data
	docker compose down

clean: ## Stop and delete volumes — destroys local data
	docker compose down -v
```

**`--wait` is what makes `make up` usable as a prerequisite.** Without it, compose
returns as soon as the containers start, and the next command connects to a Postgres
that is not accepting connections yet — the classic "works on the second try"
flake. It only works if the services declare `healthcheck`, which is why they are
not optional.

Pin image tags (`postgres:16-alpine`, not `postgres:latest`), so a colleague does
not get a different major version next week.

Keep `down` and `clean` distinct: people run `down` many times a day, and losing the
local database each time is miserable. Deleting volumes should be something you ask
for.

## Dockerfile: multi-stage, and small on purpose

```dockerfile
# Go — build stage
FROM golang:1.23-alpine AS build
WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download                  # cached until go.sum changes

COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /bin/api ./cmd/api

# runtime stage
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /bin/api /api
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/api"]
```

```dockerfile
# Python
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY --from=build --chown=app:app /app /app
USER app
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

The principles, in order of how much they matter:

**Copy the dependency manifest before the source.** Docker caches per layer and
invalidates everything after the first change. Source changes on every commit;
dependencies change rarely. Copy `go.sum` / `uv.lock`, install, *then* copy the
source — and a code change reuses the dependency layer instead of reinstalling
everything. This single ordering usually takes a build from minutes to seconds.

**The runtime stage carries no build tools.** The Go image is a compiler plus a
module cache during build and roughly 15 MB after. A single-stage build ships the
entire toolchain to production: slower pulls, and a much larger attack surface.

**Run as non-root.** `distroless:nonroot` and the explicit `useradd` both exist for
that. A container running as root is one escape away from root on the host, and
plenty of scanners will fail the image outright.

**`.dockerignore` is not optional:**

```
.git
.venv
node_modules
__pycache__
*.pyc
.pytest_cache
.ruff_cache
dist
bin
.env
```

Without it the build context includes `.git` and every local artifact — slow, and
`.env` ends up inside the image. Shipping a `.env` into an image is a real way
credentials leak.

## Hot reload, when the app must run in a container

Preferably the app runs on the host and only its dependencies are containerized.
When it cannot — a native dependency, or parity with production matters — add a
development stage and bind-mount the source:

```yaml
  api:
    build:
      context: .
      target: dev            # a stage that includes the reload tool
    volumes:
      - .:/app               # source from the host
      - /app/.venv           # anonymous volume: do NOT shadow the venv
    environment:
      DATABASE_URL: postgresql+asyncpg://postgres:local@postgres:5432/app
    depends_on:
      postgres: { condition: service_healthy }
```

The second volume is the part that trips people: bind-mounting `.` over `/app`
hides everything the image built there, including the virtualenv or
`node_modules`. The anonymous volume masks that one path back, so the container
keeps its own.

Note `postgres:5432` rather than `localhost:5432` — inside the compose network,
services reach each other by service name. Using `localhost` in a containerized app
is the most common "connection refused" in local setups.

`depends_on` with `condition: service_healthy` waits for the healthcheck rather than
just for the container to exist.

## README

Once the Makefile exists, the README's setup section shrinks to almost nothing —
which is the point, because a short one stays true:

```markdown
## Getting started

    make setup     # install dependencies and tools
    make up        # start postgres and redis
    make migrate
    make run

`make help` lists everything else.
```

## Checklist

- `.DEFAULT_GOAL := help`, and a `help` target driven by `## ` comments
- `.PHONY` on every target that is not a real file
- `.SHELLFLAGS := -eu -o pipefail -c`
- Targets named the same as in every other repository
- `make check` is exactly what CI runs
- `run` and `test-all` depend on `up`, not on people remembering
- Compose declares healthchecks; `up` uses `--wait`
- `down` keeps data; only `clean` destroys volumes
- Image tags pinned, never `latest`
- Multi-stage build, dependency manifest copied before source
- Runtime stage without build tools, running as non-root
- `.dockerignore` excludes `.git`, `.env` and local artifacts

Production build and publication in CI belong to `cicd-pipelines`; this skill covers
the image itself and the machine it is developed on.
