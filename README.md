# skill-code

House engineering standards, packaged as Claude Code skills.

Two questions drove the layout. **What gets enabled together?** — every installed
skill's `description` stays permanently in context, so skills irrelevant to the
current project are expensive. And **what depends on the language?** — gitflow,
Terraform and review criteria are identical in any stack; architecture and testing
are not.

Hence three plugins:

| Plugin | Skills | Install when |
|---|---|---|
| `engineering-base` | 8, language-agnostic | always |
| `python-fastapi` | 3, stack-specific | the project is Python/FastAPI |
| `go-gin` | 3, stack-specific | the project is Go with gin and sqlc |

Install `engineering-base` plus the one stack you are in — never both stacks. A
Python project loads 11 descriptions, not 14.

A new stack becomes another plugin alongside, reusing `engineering-base` unchanged.

## Installation

```bash
/plugin marketplace add CaiqueGo/skill-code
/plugin install engineering-base@skill-code
/plugin install python-fastapi@skill-code   # or go-gin@skill-code
```

Without plugins — copy the skills into a specific project:

```bash
cp -r plugins/engineering-base/skills/* .claude/skills/
cp -r plugins/python-fastapi/skills/* .claude/skills/   # or go-gin
```

## The skills

### engineering-base

| Skill | Triggers when |
|---|---|
| `project-context-discovery` | entering a repo for the first time — **run before the others** |
| `pr-review` | reviewing a diff or PR, or before opening your own |
| `code-security` | auth, secrets, injection, sensitive data, dependency CVEs |
| `concurrency-correctness` | double charges, lost updates, deadlocks, idempotency |
| `gitflow` | branch, commit, merge, release, hotfix, revert |
| `terraform-standards` | any `.tf` — AWS in depth, GCP and Azure by equivalence |
| `cicd-pipelines` | anything under `.github/workflows/` |
| `dev-environment` | Makefile, docker compose, Dockerfile, "how do I run this" |

### python-fastapi

| Skill | Triggers when |
|---|---|
| `fastapi-architecture` | organizing code, "where does this rule go", new module |
| `fastapi-async` | `async`/`await`, or symptoms of slowness and hanging |
| `python-testing` | writing or fixing tests, unit through e2e |

### go-gin

| Skill | Triggers when |
|---|---|
| `go-architecture` | packages, gin handlers, sqlc as the data layer, import cycles |
| `go-concurrency` | goroutines, channels, context, races, leaks, shutdown |
| `go-testing` | table tests, httptest, testcontainers against real Postgres |

The two stack plugins mirror each other on purpose: architecture, concurrency,
testing. What differs is what each language actually gets wrong — Python's single
event loop versus Go's shared memory are not the same problem, so they are not the
same skill.

## Agents

A skill is instructions injected into the current conversation; an agent runs in
its own context and returns a report. An agent is worth the cold start only when it
buys context isolation, parallelism, independence from your reasoning, or
restricted tools.

**Shipped** — installed with the plugin:

| Agent | Ships with | Does |
|---|---|---|
| `context-discovery` | `engineering-base` | investigates a repository and returns a ~40-line brief of its real conventions |

**Maintenance** — in `.claude/agents/`, for working on *this* repository. They are
deliberately outside the plugins, so nobody installing the skills receives them:

| Agent | Does |
|---|---|
| `skill-consistency-auditor` | reads all 14 skills and reports where they contradict each other, where a cross-reference is stale, and where an example violates a rule stated elsewhere |
| `skill-value-checker` | answers a question with the skills hidden from it, so you can compare and find the parts a skill would have produced anyway |

`skill-consistency-auditor` exists because the failure mode of a package this size
is two skills telling the reader different things about the same subject —
sessions, idempotency, timeouts, which skill owns what. Several thousand lines do
not fit in one context, so nobody can check it by reading.

`skill-value-checker` is the one that can *only* be an agent. The test for whether
a skill section earns its place is "would the model have said this anyway?", and
you cannot answer it once you have read the skill. A fresh context can. It is
forbidden from opening any file under `plugins/`, and reports which files it read
so you can confirm the answer is uncontaminated.

`context-discovery` buys the first and the last. A thorough investigation reads the
manifest, the tree, a full vertical slice, two tests and the CI config — dozens of
files to produce forty lines. Inline, all of that stays in the conversation for the
rest of the session. And with no write tools, an investigation cannot modify the
code it is describing.

The split is **the agent gathers, the skill decides**: collecting facts is
mechanical and context-heavy, while deciding which convention wins depends on the
task at hand and stays in the main conversation.

`project-context-discovery` governs the others: it reads the project's **real**
conventions and decides what wins when they diverge from the standards here. Without
it, the rest impose a template and the repository ends up with two conventions
living side by side — which is worse than one bad convention.

## Making the skills actually fire

A `description` only makes a skill *available* — the model still decides whether to
consult it, and for a task it believes it can handle alone ("create a GET
endpoint") it often does not. Measured here, that decision happens far less often
than it should.

`templates/CLAUDE.md` closes that gap. Copy it to the root of a project:

```bash
cp templates/CLAUDE.md /path/to/your-project/CLAUDE.md
```

`CLAUDE.md` is loaded into context unconditionally, on every session, so it does not
depend on the model choosing to look. It turns the standards from a suggestion into
a rule. Trim it to the sections that apply and fill in the project facts — every
line costs tokens on every request, so detail belongs in a skill, not in there.

## Scripts

Deterministic work becomes a script instead of an instruction: it runs in
milliseconds, produces identical output every time, and costs no context.

```bash
# map stack, layout, CI gates and churned files of a repository
python plugins/engineering-base/skills/project-context-discovery/scripts/map_project.py

# scaffold a complete domain module across the manager-pattern layers
python plugins/python-fastapi/skills/fastapi-architecture/scripts/new_module.py invoice --root src
```

## Contributing

```bash
python scripts/validate_skills.py
```

Checks skills and agents: frontmatter, `name` matching the directory or filename, a
description long enough to route to, references pointing at nonexistent files,
oversized `SKILL.md`, and agents with no `tools:` restriction. Exits 1 on errors —
wire it into CI.

`evals/` measures something the validator cannot: whether a skill actually fires on
the prompts it should, and stays quiet on the ones it should not. Read
`evals/README.md` before running it — a full run starts hundreds of `claude -p`
sessions and costs accordingly. Any change to a `description` should be checked there.

### Writing a new skill

What most determines quality, in order:

1. **The `description` is 90% of the work.** It is the only thing that decides whether
   the skill triggers. List the phrases a person actually types, not what the skill
   contains. Models tend to under-trigger skills — be explicit and slightly pushy.
2. **The body decides, the reference elaborates.** Keep `SKILL.md` under 500 lines: it
   enters context in full every time the skill triggers, while `references/` is read
   only when needed.
3. **Explain the why.** "Do not import `fastapi` in the manager, because the day that
   rule runs in a queue consumer it has to work unchanged" makes the rule apply in
   situations you did not foresee. `NEVER do X` only works for the literal case.
4. **Deterministic becomes a script; judgment becomes prose.**
5. **Prefix by stack.** `fastapi-architecture`, not `architecture` — otherwise two
   skills compete for the same trigger.

### Language

Everything in this repository is written in English — file names, directory names,
skill bodies and code identifiers — regardless of the language the team speaks day to
day. Skill descriptions in English trigger normally on prompts in other languages,
because the match is semantic rather than literal.
