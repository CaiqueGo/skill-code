---
name: cicd-pipelines
description: CI/CD pipelines in GitHub Actions — workflow structure, quality gates, caching, matrices, keyless cloud authentication via OIDC, environments with approval, deploy and rollback, and securing the pipeline itself. Use ALWAYS when a file under `.github/workflows/` is involved, when creating or fixing a pipeline, when adding a check to CI, and when the request is "set up CI", "why is the build slow", "the workflow failed", "how do I deploy automatically", "add this test to the pipeline", "I need approval before prod", or when configuring cloud credentials for CI. Trigger also when reviewing a PR that changes a workflow — a pipeline change is a change to the attack surface.
---

# GitHub Actions pipelines

## What the pipeline is for

Giving a **fast and trustworthy** answer about whether a change can go to production.
Two properties, and both matter:

- **Fast**: past ~10 minutes people stop waiting and switch tasks; the real cost is
  the lost context, not the machine time.
- **Trustworthy**: a pipeline that fails randomly trains the team to hit "re-run"
  without reading. From then on, genuine failures get re-run too.

A flaky test is more expensive than a missing one. Fix it or delete it — do not live
with it.

## Structure

Split by **trigger and speed**, not by topic:

```
.github/workflows/
  ci.yml          # PR and push: lint, types, tests. Fast.
  security.yml    # PR + scheduled: dependencies, secrets, SAST
  deploy.yml      # push to main or tag: build, publish, deploy
  terraform.yml   # PR touching infra/: fmt, validate, plan (comments the plan)
```

The reason for splitting: `ci.yml` runs on every push and must stay lean.
`security.yml` also runs on a schedule, because CVEs are published after merge.

## CI

```yaml
name: ci

on:
  pull_request:
  push:
    branches: [main]

# cancel an older run of the same PR when a new push arrives
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read        # the minimum; widen per job when needed

jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true
          cache-dependency-glob: uv.lock

      - run: uv sync --frozen        # fails if the lockfile is stale
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run mypy src
      - run: uv run lint-imports     # layer contract (import-linter)

  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_PASSWORD: postgres
        options: >-
          --health-cmd pg_isready --health-interval 5s --health-retries 10
        ports: ["5432:5432"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: { enable-cache: true }
      - run: uv sync --frozen
      - run: uv run pytest --cov=src --cov-report=xml --cov-fail-under=80
        env:
          DATABASE_URL: postgresql+asyncpg://postgres:postgres@localhost:5432/postgres
```

The points that make a difference:

- **`concurrency` with `cancel-in-progress`** — without it, five pushes in a row
  trigger five full runs and the queue grows.
- **`permissions` at the top, minimal** — an older repository may default to `write`
  on everything, and then a compromised workflow can publish releases.
- **`--frozen`** — fails if `uv.lock` is out of sync with `pyproject`. Without it, CI
  resolves different versions than you do and tests something else.
- **`services`** for the database is faster than testcontainers in CI, because the
  runner pulls the image in parallel with checkout.
- **Separate jobs** run in parallel. `quality` and `test` do not depend on each other;
  merged into one job, the time is the sum.

## Caching

A wrong cache is worse than no cache: it produces results that do not reproduce. The
key must contain **everything that changes the content**:

```yaml
- uses: actions/cache@v4
  with:
    path: ~/.cache/pip
    key: ${{ runner.os }}-py${{ matrix.python }}-${{ hashFiles('**/uv.lock') }}
    restore-keys: ${{ runner.os }}-py${{ matrix.python }}-
```

`restore-keys` allows partial reuse when the lockfile changes — without it, every
dependency change downloads everything again.

Do not cache build artifacts of your own code: they change every commit and the hit
rate is zero.

## Matrices — only when you genuinely support it

```yaml
strategy:
  fail-fast: false            # one failing Python must not hide the others' results
  matrix:
    python: ["3.11", "3.12", "3.13"]
```

A 3-version matrix triples time and cost. It is justified for a **library**, which
runs on someone else's Python. For a service you deploy yourself on a fixed version,
test only that one — the matrix there is pure waste.

## Deploying without static secrets

A cloud access key stored in `secrets` is a long-lived credential: it leaks in logs,
outlives whoever created it, and nobody rotates it. OIDC issues short-lived
credentials per run:

```yaml
name: deploy

on:
  push:
    branches: [main]

permissions:
  id-token: write        # required for OIDC
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: production        # manual approval configured in the UI
    steps:
      - uses: actions/checkout@v4

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::123456789012:role/github-deploy
          aws-region: us-east-1
          # no keys; the job's OIDC token assumes the role

      - uses: aws-actions/amazon-ecr-login@v2
        id: ecr

      - name: build and push
        run: |
          IMAGE=${{ steps.ecr.outputs.registry }}/payments:${{ github.sha }}
          docker build -t "$IMAGE" .
          docker push "$IMAGE"
          echo "IMAGE=$IMAGE" >> "$GITHUB_ENV"

      - name: deploy
        run: |
          aws ecs update-service --cluster prod --service payments \
            --force-new-deployment --task-definition "$(...)"
          aws ecs wait services-stable --cluster prod --services payments
```

On the cloud side, the role must restrict `sub` to the repository **and** the branch —
see `terraform-standards/references/aws.md`. GCP and Azure have equivalents in their
respective files.

**Tag the image with `github.sha`, never `latest`.** With `latest`, "what is in
production" is not an answerable question, and a rollback has no target.

`aws ecs wait services-stable` is what makes the job honest: without it the workflow
goes green on *requesting* the deploy, even if the new task never starts.

## Environments and approval

`environment: production` on a job activates what the GitHub UI configures: required
reviewers, wait timers, and secrets scoped to that environment only.

It is the right mechanism for production — the approval is recorded, and the
production secret is not reachable from a PR workflow.

## Terraform in the pipeline

```yaml
jobs:
  plan:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read
      pull-requests: write        # to comment the plan
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.TF_PLAN_ROLE }}   # READ-ONLY role
          aws-region: us-east-1
      - uses: hashicorp/setup-terraform@v3
      - run: terraform init
      - run: terraform fmt -check -recursive
      - run: terraform validate
      - run: terraform plan -no-color -out=tfplan
      - run: terraform show -json tfplan > plan.json
      - name: fail if anything would be destroyed
        run: |
          DESTROYED=$(jq -r '.resource_changes[]
            | select(.change.actions[] == "delete") | .address' plan.json)
          if [ -n "$DESTROYED" ]; then
            echo "::warning::resources would be destroyed:"; echo "$DESTROYED"
          fi
```

Two distinct roles: **plan uses a read-only role**, `apply` uses a write role and only
runs in a job with an approved `environment`. A fork PR must not be able to do
anything beyond reading.

## Securing the pipeline itself

A workflow is code with access to secrets — and it is reviewed less carefully than
the rest.

- **Pin third-party actions by SHA**, not by tag. Tags are mutable: `@v1` may point at
  a different commit tomorrow.
  ```yaml
  - uses: actions/checkout@08eba0b27e820071cde6df949e0beb9ba4906955  # v4.3.0
  ```
- **Never use `pull_request_target`** with a checkout of the PR's code. That
  combination runs fork code **with the repository's secrets** — the most exploited
  flaw in Actions.
- **Never interpolate user input into `run:`**. PR titles and branch names are
  controlled by whoever opens them:
  ```yaml
  - run: echo "${{ github.event.pull_request.title }}"   # shell injection
  - run: echo "$TITLE"                                    # correct
    env: { TITLE: ${{ github.event.pull_request.title }} }
  ```
- **Explicit `permissions`** on every workflow, minimal.
- Never `echo` a secret; GitHub's masking does not catch transformed values (base64,
  concatenated).

## When it is slow

In order of return:

1. **Parallelize jobs** — what does not depend should not wait
2. **`concurrency` with cancellation** — stop running what nobody will read
3. **Dependency caching** with a correct key
4. **Trim the matrix** to what you actually support
5. **Split by path** — `paths:` so the full suite does not run when only the README
   changed
6. **A bigger runner** — last resort; it costs money and does not fix a bad design

## Checklist

- Explicit, minimal `permissions` on every workflow
- `concurrency` with `cancel-in-progress` on CI
- OIDC instead of static cloud keys
- Third-party actions pinned by SHA
- No `pull_request_target` with a checkout of PR code
- User input passed via `env`, never interpolated into `run:`
- Images tagged by commit SHA, never `latest`
- Deploy waits for stabilization before reporting success
- Production behind an `environment` with approval
- Dependency scanning also scheduled, not only on PRs
