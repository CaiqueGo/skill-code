---
name: gitflow
description: Branching, commits and releases — choosing between trunk-based and GitFlow, branch naming, Conventional Commits, semantic versioning, rebase vs merge, hotfixes and reverts. Use ALWAYS when the task involves git beyond the trivial: creating a branch, writing a commit message, opening a PR, resolving a conflict, preparing a release or tag, reverting something that broke production, cleaning history before review, or when the user asks "which flow should we use", "how do I name this branch", "squash or merge", "how do I do the hotfix". Trigger also when writing the commit message for any change you just made.
---

# Git flow

## Pick the flow by deploy frequency

This is the only variable that decides, and using the wrong flow creates constant
friction:

| You deploy... | Use | Why |
|---|---|---|
| several times a day | **trunk-based** | long branches become conflict and deferred integration |
| every 1–2 weeks | **GitHub Flow** | one branch per change, `main` always shippable |
| in versions, supporting old ones | **GitFlow** | `develop` + `release/*` earn their cost |

**For a web service with continuous deployment, trunk-based or GitHub Flow.**
GitFlow was designed for versioned, distributed software (installers, libraries,
firmware). Applied to a service that ships three times a day, it only adds `develop`
as a second place where merges can conflict — with no matching benefit.

The rest of this skill assumes GitHub Flow, which covers most cases. Full GitFlow is
in `references/gitflow-full.md`.

## GitHub Flow

```
main ──●──────●──────────●────────●──▶  always shippable, protected
        \    /            \      /
         ●──●              ●────●        short feature branch, PR, squash
```

Rules that make this work:

- `main` protected: no direct pushes, PR with review and green CI
- A branch is born from `main` and lives **less than 3 days** — beyond that, the cost
  of reintegrating grows faster than the work accumulated
- Deploy from `main`, not from the branch
- A large feature goes behind a feature flag, in small pieces that already land in
  `main`. That is what lets the branch stay short without shipping half a feature.

## Branch naming

```
<type>/<short-hyphenated-description>
<type>/<ticket>-<description>

feat/pix-payment
fix/billing-gateway-timeout
chore/bump-sqlalchemy-2.0
hotfix/connection-pool-leak
```

Types: `feat`, `fix`, `chore`, `refactor`, `docs`, `test`, `hotfix`.

Lowercase with hyphens — some tooling handles uppercase inconsistently across
filesystems. No person's name (`caique/test` tells the reviewer nothing six months
later).

## Conventional Commits

```
<type>(<scope>)!: <imperative summary, lowercase, no trailing period>

<body: why, not what>

<footer: BREAKING CHANGE, Refs #123>
```

```
feat(orders): add pix payment method

The current gateway does not support pix and the commercial team needs it
for the October launch. Implemented as a separate strategy so the card
flow, which is stable, is untouched.

Refs #482
```

The format is not bureaucracy: it is what allows generating a changelog and deriving
the version automatically. `feat` → minor, `fix` → patch, `!` or `BREAKING CHANGE:` →
major.

**The body explains the why.** The *what* is in the diff — restating it in prose is
redundant. The *why* is nowhere else, and it is exactly what you will want a year
from now when `git blame` leads you to that line.

Imperative mood because it completes the sentence "this commit ...": "add pix
payment method", not "added" or "adding".

Common commits that should not exist: `wip`, `fixes`, `changes`, `.`, `address PR
comments`. If the branch history looks like that, clean it before asking for review
(see below).

## Rebase, merge and squash

Each solves a different problem:

**`rebase` to update your branch from `main`.** Keeps history linear and your
commits on top:

```bash
git fetch origin
git rebase origin/main
```

Non-negotiable rule: **never rebase a branch someone else is already using.** Rebase
rewrites hashes; whoever had the old branch ends up with divergent history and the
fix is manual.

**`squash` to land the PR on `main`.** One change = one commit on `main`. The history
of `main` becomes the list of changes that shipped, not a diary of how they were
written — and `git revert` of one commit undoes the whole feature.

When a PR genuinely carries distinct, well-written commits (migration, then code,
then cleanup), a **merge commit** preserves that structure and is worth more than a
squash.

**Cleaning a branch before asking for review:**

```bash
git rebase -i origin/main    # squash the "wip"s, reorder, rewrite messages
```

In a non-interactive session `git rebase -i` is unavailable — in that case, redo the
commits with `git reset --soft origin/main` followed by fresh commits, or leave the
squash for merge time.

## Releases and versioning

Semantic versioning, derived from the commits:

```
MAJOR.MINOR.PATCH     1.4.2
  │     │     └── fix: backward-compatible fix
  │     └──────── feat: backward-compatible feature
  └────────────── BREAKING CHANGE: contract break
```

For an internal service with continuous deployment, a date tag (`2026.09.14`) is
usually more honest than semver: nobody consumes your API by version number, and
"what is in production" is the real question. Semver is for things others integrate
against — a library, an SDK, a public API.

```bash
git tag -a v1.4.2 -m "release 1.4.2"
git push origin v1.4.2
```

Annotated tag (`-a`), not lightweight: it records author, date and message.

## Hotfix

```bash
git checkout main && git pull
git checkout -b hotfix/connection-pool-leak
# the smallest possible fix — no drive-by improvements
git commit -m "fix(db): close the session on the checkout error path"
# PR with expedited review, full CI, merge, tag, deploy
```

Two things urgency does not justify: **skipping CI** and **fixing something else
while you are in there**. The hotfix must be trivially reviewable and trivially
revertible — that is the only property that matters at 3am.

If the flow has `develop`, the hotfix goes to `main` **and** to `develop`, otherwise
the next release brings the regression back.

## Reverting

```bash
git revert <sha>              # creates a commit that undoes — safe on a public branch
git revert -m 1 <merge-sha>   # reverting a merge commit
```

Use `git revert`, not `git reset`, on any shared branch. `reset` rewrites history
others already have.

**Revert first, investigate second.** With production broken, the goal is restoring
service; understanding the cause is the next task, done without pressure. The revert
is itself revertible.

## Before opening the PR

- Branch rebased on `main`, CI green locally
- Commit messages that explain the why; no `wip`
- `git diff main...HEAD` read in full, the way the reviewer will read it
- No secret, `print`, `TODO` or local file in the diff
- Description states the why and how to test
