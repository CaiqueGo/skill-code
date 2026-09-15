# Full GitFlow

Read this **only** when the project genuinely requires GitFlow: versioned,
distributed software, or simultaneous support for more than one version in
production (customer A on 2.x, customer B on 3.x). For a web service with continuous
deployment, GitHub Flow solves it with less friction — see the `SKILL.md`.

## The branches

```
main      ●────────────●──────────────────●────▶   releases only, each tagged
           \          /                  /
release     \    ●──●──●               /           stabilization, fixes only
             \  /       \             /
develop  ●────●──●───●───●───●───●───●──────────▶  integration
          \      /     \     /
feature    ●────●       ●───●                      born from and back to develop
```

| Branch | Born from | Merges back to | Lives |
|---|---|---|---|
| `feature/*` | `develop` | `develop` | until the feature is done |
| `release/*` | `develop` | `main` **and** `develop` | days, stabilization only |
| `hotfix/*` | `main` | `main` **and** `develop` | hours |
| `develop` | — | — | permanent |
| `main` | — | — | permanent |

## The release cycle

```bash
# 1. open the branch when scope is frozen
git checkout develop && git pull
git checkout -b release/1.5.0

# 2. only bug fixes go in here. New features go to develop and wait for 1.6.
#    (this is the rule people break, and it is what makes releases never end)

# 3. close it: merge to main WITH A TAG
git checkout main && git merge --no-ff release/1.5.0
git tag -a v1.5.0 -m "release 1.5.0"

# 4. and back into develop, otherwise the fixes are lost
git checkout develop && git merge --no-ff release/1.5.0
git branch -d release/1.5.0
```

**Step 4 is the most forgotten** and the most expensive: without it, every fix made
during stabilization disappears and reappears as a bug in the next release.

Always `--no-ff`, so the merge stays visible as a point in history — that is what
lets you revert an entire release with one command.

## Hotfix

```bash
git checkout main && git pull
git checkout -b hotfix/1.5.1-fix-timeout
# minimal fix
git checkout main && git merge --no-ff hotfix/1.5.1-fix-timeout
git tag -a v1.5.1 -m "hotfix 1.5.1"
git checkout develop && git merge --no-ff hotfix/1.5.1-fix-timeout
```

If a `release/*` branch is open at that moment, the hotfix goes into it as well —
otherwise the release under stabilization ships with the regression already in it.

## Supporting old versions

The case that genuinely justifies GitFlow. A maintenance branch per version line:

```
support/1.x   ●────●────●──▶     ported fixes, tags 1.5.1, 1.5.2
main          ●────●────●──▶     current line, tags 2.0.0, 2.1.0
```

A fix is born on the oldest branch that needs it and moves up (`cherry-pick` into
newer lines), never the other way around — the reverse drags in code the old version
should not have.

## The costs, so you decide consciously

- **Two places to conflict**: every merge happens in `develop` and again in `main`.
- **Deferred integration**: a long-lived `feature/*` only meets everyone else's code
  at the end, when the conflict is large.
- **Forgotten merge-back**: a `release`/`hotfix` that never returns to `develop` is
  the #1 cause of regressions reappearing.
- **Continuous deployment gets awkward**: if you ship several times a day, `release/*`
  has nothing to stabilize.

If at least two of those hurt in your project today, the flow is wrong for it.

## Automation that makes the cost bearable

- Merge-back of `release`/`hotfix` into `develop` done by automation, not memory (a
  GitHub Actions job opening the PR automatically)
- Protection on `main` and `develop`: no direct pushes, CI required
- Tag created by the pipeline from the merge into `main`, not by hand
- Changelog generated from Conventional Commits
