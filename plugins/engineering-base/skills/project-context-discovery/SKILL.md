---
name: project-context-discovery
description: Discovers a project's real conventions before writing or reviewing code in it — layer structure, naming, test patterns, error handling, dependency injection and CI gates — and decides which convention wins when the project diverges from the house standards. Use ALWAYS when entering a repository for the first time in a session, before applying any architecture standard, before creating a new module/endpoint/test, and when reviewing a PR in a project you have not read. Trigger also when the user says "follow the project's pattern", "make it like the rest", "analyze this repo", "how is this project organized", or when a house standard conflicts with what the code already does. Run this before the architecture, testing and review skills.
---

# Project context discovery

## Why this comes first

The other skills describe the house standard. A real project is almost never
exactly in it: it is legacy, it predates the standard, or it made a different
decision for a reason written down nowhere.

Applying the house standard on top of a project that does things differently
produces the worst outcome available — **two conventions living in the same
repository**. Whoever reads it next cannot tell which is right, and both spread.
Consistently "wrong" code is cheaper to maintain than a repository with two
standards, because the first is fixed by a mechanical refactor and the second needs
judgment file by file.

So: **discover first, propose second.**

## Delegate the investigation

Spawn the **`context-discovery`** agent and work from its report.

The reason is context, not convenience. A thorough investigation reads the
manifest, the directory tree, a full vertical slice, two tests and the CI config —
dozens of files, to produce about forty lines of conclusions. Read inline, all of
that sits in this conversation for the rest of the session, crowding out the work
you are actually here to do. The agent absorbs the reading and returns only the
brief.

It is also read-only by construction — it has no write tools — so an investigation
cannot accidentally change the code it is describing.

Give it the scope you care about. "Analyze this repository" is fine for a first
pass; "report the conventions of the billing domain, I am adding an endpoint there"
gets a sharper answer.

**If subagents are unavailable** in this environment, do the investigation inline
in this order, and expect it to cost context: run
`scripts/map_project.py`; read `CLAUDE.md` and any ADRs; read the whole vertical
slice of the **most recently changed** domain; read one unit and one integration
test; then grep to verify any boundary claim before relying on it.

## Reading the report

Two fields decide how much weight to give each line:

**The evidence count.** `[4/4 domains]` is a convention. `[2 examples]` is a
coincidence — two files that match may have been written the same afternoon by the
same person. Below three independent examples, treat it as weak and prefer the
house standard for new code, saying that you did.

**The inconsistencies section.** When modules disagree, follow the **more recent**
code: that is the direction the team is moving. A project mid-migration has two
patterns on purpose, and picking the older one moves it backwards.

If the report says a claim was not verified, verify it before building on it.

## Which convention wins

This is the judgment the agent deliberately does not make, because it depends on
what you are about to do. Three cases, and they do not blur:

**1. The project wins — naming, style, organization.**
If the project calls `Service` what the house calls `Manager`, write `Service`. If
files are `routes.py` and not `router.py`, use `routes.py`. This is taste, and
consistency beats taste. Do not flag the divergence on every file; it is noise.

**2. The house standard wins — correctness and security.**
Secrets in code, concatenated SQL, a missing timeout on an external call, a
swallowed exception, an unauthenticated endpoint. Here "the whole project does it
this way" is not a justification — it is a description of the problem. Flag it, and
fix it within the scope you are already touching.

**3. Grey zone — the architecture diverges structurally.**
A route reaching into the repository directly, business rules in the handler, the
ORM model used as the domain entity. Not a bug, not taste: debt.

The rule here is **do not refactor as a side effect**. Follow the existing
convention in the code you are writing, and record the divergence. A PR that
changes what was asked *plus* the architecture of a layer is unreviewable, and that
is how the second convention is born. If the debt genuinely blocks the task, say so
and ask — refactoring is the code owner's call.

## When there is no convention

A new project, or one where every module does its own thing. Then the house
standard applies in full — that is the case it was written for. Say so, because it
is a decision rather than a silent default:

> The agent found no consistent convention (3 modules, 3 organizations).
> I will follow the house standard (`fastapi-architecture`) for new code.

## Revalidation

The brief holds for the session. Spawn the agent again, scoped to the new domain,
when you move to a different part of a large repository — conventions frequently
differ per module, and one of them is often the newer way the team is migrating
toward.
