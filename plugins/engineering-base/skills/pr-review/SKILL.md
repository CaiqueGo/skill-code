---
name: pr-review
description: Pull request review with judgment — what to look for, in what order, how to classify severity and how to write the comment. Use ALWAYS when the request involves reviewing code: "review this PR", "look at this diff", "what do you think of this change", "do a code review", analysis of a `git diff`, a GitHub/GitLab PR link, or before opening your own PR ("is this ready for review?"). Covers correctness, concurrency, security, tests, database migrations and API compatibility. Trigger also when the user asks for an opinion on a change they just made themselves.
---

# Pull request review

## What review is for

Lint and the type checker already catch formatting, unused imports and wrong types.
If review spends time on those, the CI is misconfigured — fix the CI, not the
process.

Human review exists for what no tool sees:

1. **The change does what it says** — and what it says is what was asked for
2. **Edge cases** the author did not consider (null, empty, concurrent, duplicate)
3. **Systemic consequence** — database load, contract breakage, cost, latency
4. **Reversibility** — if this goes wrong in production at 3am, can it be rolled back?

## Reading order

Reviewing file by file in diff order is how a reviewer misses the structural
problem: by the time they reach file 12, they have already accepted the premise of
the previous 11.

**1. Description and scope.** What the PR claims to do. If the description does not
explain the *why*, ask for that first — without a stated intent there is no way to
judge whether the solution is appropriate, only whether it is pretty.

**2. Size.** Above ~400 lines of real change, review quality drops sharply, and the
most useful response is to ask for a split before reviewing. Refactor + feature in
one PR is the most common case: ask to separate them, because in a mixed diff nobody
can distinguish the behavior change from the code movement.

**3. The tests, before the code.** Tests state what the author believes the change
does. Reading the test first and the code second reveals divergence between intent
and implementation — and immediately shows which edge case was not considered.

**4. The main path**, outside in: route → business layer → persistence.

**5. The "boring" files** that get skipped and concentrate incidents: migrations,
configuration, new dependencies, CI workflows, IaC.

## What to look for

**Correctness**
- Swallowed error (`except Exception: pass`) or an overly broad catch
- Unhandled `None` on a path that can now return `None`
- Off-by-one, float comparison, unstable sort
- Mutable state shared across requests (mutable default, global, module-level cache)

**Concurrency** (see `fastapi-async` for detail)
- Blocking call inside `async def`
- `gather` with no bound over a variable-length list
- Database session shared across concurrent tasks
- Read-decide-write with no transaction or lock — the classic double charge

**Security** (see `code-security`)
- Secret, token or credentialed URL in the diff
- SQL built by concatenation or f-string
- New endpoint with no authentication/authorization
- Sensitive data in logs or in an error message returned to the client
- New dependency: is it maintained? does it have a CVE? is it actually needed?

**Database**
- Destructive migration (drop column, type change) with no compatibility step
- Missing index on a new column that appears in `WHERE` or `JOIN`
- Blocking `ALTER TABLE` on a large table
- N+1: a query inside a loop
- Migration with no path back

**API contract**
- Removed or renamed field, new required field, changed type, new enum value — all
  of these break clients. Backward compatibility is the default; breaking it
  requires a version or a prior agreement.
- Changed status code or error shape

**Tests**
- Is there a test for the error path, not just the happy one?
- Would the test fail if the change were reverted? (if not, it tests nothing)
- Tests depending on the clock, on ordering, or on the network

**Operations**
- Can this be observed in production? (logs with context, metrics)
- Feature flag for a risky behavior change
- What happens if the new external service is down?

## Severity — and why it is mandatory

A comment without severity makes the author treat a variable-naming suggestion and a
concurrency bug with the same urgency. In practice they fix the easy ones first, and
the hard one dissolves among twenty comments.

Prefix **every** comment:

| Prefix | Means | Blocks merge? |
|---|---|---|
| `blocking:` | bug, security flaw, contract break | yes |
| `important:` | will hurt soon; decide consciously | no, but respond |
| `suggestion:` | improvement, optional | no |
| `question:` | I do not follow, explain | no |
| `praise:` | this is good | no |

`question:` is the most underrated. A good share of what looks like an error is
context the reviewer lacks — asking before asserting avoids the exchange where the
reviewer is wrong and spent three paragraphs proving otherwise.

And `praise:` is not politeness: explicitly marking the good pattern is how it
spreads to the rest of the team.

## How to write the comment

State the problem, explain the consequence, offer a path. All three together —
without the second it is personal taste; without the third it is just criticism.

```
blocking: `get_or_create` does a SELECT and then an INSERT with no transaction.
Two concurrent requests for the same email create two users — this already
happened in the invite flow back in January.

Suggest `INSERT ... ON CONFLICT DO NOTHING` followed by the SELECT, or a
unique constraint plus IntegrityError handling.
```

Comment on the code, never on the person. "this function is doing three things"
rather than "you did this wrong". The difference looks cosmetic and is not: the
first discusses the code, the second invites defensiveness.

When there are more than three comments on the same theme, group them into one
general comment instead of repeating per line — twenty identical notes bury the
finding that matters.

## The verdict

Always end with an explicit decision, because a PR without a verdict stalls:

```markdown
## Review

**Verdict**: request changes  (approve | approve with comments | request changes | request split)

**Summary**: the billing logic is correct and well tested. Two concurrency problems
in the creation flow and one migration with no path back.

**Blocking** (2)
1. `orders/service.py:88` — read-and-write with no transaction, allows double charge
2. `migrations/003_drop_legacy.py` — column drop with no compatibility step;
   a partial deploy takes down the old instances

**Important** (1)
1. `orders/repository.py:42` — query inside a loop; N+1 with ~200 items per order

**Suggestions** (3) — commented inline, all optional

**Good**: the tests in `test_billing.py` cover the rounding edge cases well.
```

## Reviewing your own PR before opening it

Same checks, done by you first. This is the highest-return moment: a problem you
find yourself costs minutes; the same problem found by the reviewer costs a
round-trip of hours.

- Read the whole diff the way the reviewer will — `git diff main...HEAD`
- Any leftover `print`, `TODO`, commented-out code, local test file?
- Does the description explain the *why*, not just the *what*?
- Can this be split into two PRs? If it can, split it.
