---
name: project-context-discovery
description: Discovers a project's real conventions before writing or reviewing code in it — layer structure, naming, test patterns, error handling, dependency injection and CI gates. Use ALWAYS when entering a repository for the first time in a session, before applying any architecture standard, before creating a new module/endpoint/test, and when reviewing a PR in a project you have not read. Trigger also when the user says "follow the project's pattern", "make it like the rest", "analyze this repo", "how is this project organized", or when the house standard conflicts with what the code already does. This skill decides which convention wins — run it before the architecture, testing and review skills.
---

# Project context discovery

## Why this comes first

The other skills describe the house standard. A real project is almost never
exactly in it: it is legacy, it was written before the standard, or it made a
different decision for a reason that is written down nowhere.

Applying the house standard on top of a project that does things differently
produces the worst possible outcome — **two conventions living in the same
repository**. Whoever reads it next cannot tell which is right, and both spread.
Consistently "wrong" code is cheaper to maintain than a repository with two
standards, because the first is fixed by a mechanical refactor and the second
requires judgment file by file.

So the rule is: **discover first, propose second.**

## Reading order

Order matters because each step shrinks what is left to read. Reading 40 source
files to find out the stack is a waste when `pyproject.toml` answers in 10 lines.

**1. Declarations — what the project says about itself**

```
CLAUDE.md, AGENTS.md, README.md, CONTRIBUTING.md, docs/adr/
pyproject.toml | package.json | go.mod | pom.xml
Makefile | justfile | taskfile.yml
.github/workflows/ | .gitlab-ci.yml
.pre-commit-config.yaml | ruff.toml | .eslintrc | setup.cfg
```

`CLAUDE.md` and ADRs are explicit instructions and take precedence over anything
you infer from the code. The lint config and the CI are the most honest sources of
all: they state what is **mandatory**, not what someone wishes were true.

**2. Shape — how the code is laid out**

Directory tree to 3 levels, ignoring `.git`, `node_modules`, `.venv`, `dist`. You
are looking for the organizing axis: by layer (`api/`, `services/`,
`repositories/`), by domain (`orders/`, `billing/`), or none (everything in `app/`).

**3. A representative slice — what the code actually looks like**

Pick the **most recently modified module**, not the first one alphabetically.
`git log` reveals which code is alive; the oldest is often precisely what the team
no longer considers exemplary.

```bash
git log --pretty=format: --name-only -50 | grep -v '^$' | sort | uniq -c | sort -rn | head -20
```

Read that part's whole slice: the route, the business layer, the data access and
the matching test. One complete vertical slice teaches more than twenty files from
the same layer.

**4. Tests** — one unit test and one integration test. The test pattern reveals the
project's real coupling better than production code does: if every test needs
`mock.patch`, there is no dependency injection, regardless of what the folder
structure suggests.

For steps 1 and 2, run `python scripts/map_project.py` — it collects the
deterministic facts (stack, package manager, lint tooling, layout, CI gates, most
churned files) in a single pass, without burning context opening files one by one.

## The context summary

Produce this summary before writing a single line. It is deliberately short — if it
does not fit here, it is detail you discover when you need it:

```markdown
## Project context

**Stack**: Python 3.12, FastAPI 0.115, SQLAlchemy 2.0 async, Postgres
**Packaging**: uv | poetry | pip-tools    **Tasks**: make test, make lint

**Organization**: by domain (`src/orders/`, `src/billing/`)
**Observed layers**: `routes.py` → `service.py` → `repository.py`
**Naming**: the business layer is called `Service`, not `Manager`

**Dependency injection**: via `Depends` into the constructor — testable without patching
**Errors**: domain exceptions in `exceptions.py`, global handler in `main.py`
**Schemas**: Pydantic separate from the entity | SQLAlchemy model used directly

**Tests**: pytest, `asyncio_mode=auto`, fixtures in `conftest.py`,
           integration via testcontainers, 80% coverage gate in CI
**CI gates**: ruff, mypy (only on `src/domain`), pytest, bandit

**Divergences from the house standard**:
- business layer is called `Service` (house uses `Manager`) — follow `Service`
- repository commits on its own (house puts the transaction in the dependency) — see below
```

## Which standard wins

This is the part that matters. Three cases, and they do not blur into each other:

**1. The project's convention wins — naming, style, organization.**
If the project calls `Service` what the house calls `Manager`, write `Service`. If
files are `routes.py` and not `router.py`, use `routes.py`. This is taste, and
consistency is worth more than taste. Do not mention the divergence on every file —
that is noise.

**2. The house standard wins — correctness and security.**
Secrets in code, concatenated SQL, missing timeout on an external call, swallowed
exception, unauthenticated endpoint. Here "the whole project does it this way" is
not a justification: it is a description of the problem. Flag it, and fix it within
the scope you are already touching.

**3. Grey zone — the architecture diverges structurally.**
A route reaching into the repository directly, business rules inside the handler,
the ORM model used as the domain entity. Not a bug and not taste: it is debt.

The rule here is **do not refactor as a side effect**. Follow the existing
convention in the code you are writing, and record the divergence in the summary. A
PR that changes what was asked *plus* the architecture of a layer is unreviewable,
and that is how the second convention is born in a repository. If the debt genuinely
blocks the current task, say so explicitly and ask first — refactoring is the code
owner's call.

## When there is no convention

A new project, or a project with no discernible pattern (every module does its own
thing). Then the house standard applies in full — that is the case it was written
for. Say so in the summary, because it is a decision, not a silent default:

> I found no consistent convention (3 modules, 3 different organizations).
> I will follow the house standard (`fastapi-architecture`) in new code.

With fewer than three examples of the same pattern, you have not observed a
convention — you have observed a coincidence. Two files that match may have been
written the same day by the same person.

## Revalidation

The summary holds for the session. Redo step 3 (the slice) when you move to a
different domain within the same repo — large projects frequently have different
conventions per module, and it is common for one of them to be the "new way" the
team is migrating toward. If you find two patterns, prefer the one in the **more
recent** code: that is the direction the team is heading.
