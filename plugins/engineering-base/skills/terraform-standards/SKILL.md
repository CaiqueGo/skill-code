---
name: terraform-standards
description: Terraform standards — module structure, state backend, environment separation, naming and tagging, variables and outputs, secrets, `for_each` vs `count`, the plan/apply cycle, drift, and what never to commit. Use ALWAYS when a `.tf`/`.tfvars`/`.hcl` file is involved, when creating or reviewing infrastructure as code, when deciding where state lives, when separating dev/staging/prod, and when the request is "create the infra for this", "review this terraform", "why does the plan want to recreate this resource", "how should I organize the modules", a state lock error, or drift between the code and what is in the cloud. Covers AWS in depth, with equivalents for GCP and Azure.
---

# Terraform standards

## The two decisions you cannot undo cheaply

Almost everything in Terraform is adjustable later. Two things are not, and getting
them wrong costs a state migration and a maintenance window:

1. **Where state lives** — changing it later requires migrating state with the
   environment frozen
2. **The boundary of each module/state** — a monolithic state cannot be split without
   `terraform state mv`, resource by resource

Decide both before the first `apply`.

## State

State maps your code to real resources. It contains **secret values in plain text** —
database passwords, generated keys — even if you marked the variable `sensitive`.
That dictates the rules:

- Never local, never committed. `*.tfstate` in `.gitignore` from the first commit.
- Remote backend with **locking** — without it, two concurrent `apply`s corrupt state.
- Encrypted at rest, versioned, with access restricted to whatever performs deploys.

```hcl
# AWS — S3 with native lockfile (no DynamoDB table needed since provider v6)
terraform {
  required_version = "~> 1.9"

  backend "s3" {
    bucket       = "my-org-tfstate"
    key          = "services/payments/prod/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"     # pin the minor; major versions change behavior
    }
  }
}
```

The `key` follows `<service>/<environment>/terraform.tfstate`. That hierarchy is what
lets you split later without inventing a new convention.

Backend equivalents in `references/gcp.md` and `references/azure.md`.

## One state per environment and per change domain

The most common structural mistake is a single state for the whole project. It
creates three problems at once: a slow `plan`, a large blast radius (an error in
network code can destroy a database), and queueing — two people cannot `apply` at
the same time.

```
infra/
  modules/                      # reusable, no backend, no hardcoded values
    network/
    ecs-service/
    rds/
  live/                         # one root per environment+domain = one state
    prod/
      network/
      data/                     # RDS, cache — rarely changes
      services/                 # ECS, Lambda — changes constantly
    staging/
      ...
```

The cut that matters is by **change frequency**. Network and database rarely change;
services change daily. Separated, the daily `apply` never touches the database state —
and there is no way a typo in `services/` triggers a `destroy` on an RDS instance.

**Do not use `terraform workspace` to separate environments.** Workspaces share the
same backend and the same code; the difference ends up in `terraform.workspace`
scattered across conditionals, and it is easy to run `apply` believing you are in
staging. Separate directories make the environment explicit in the path.

## Modules

A good module has a clear boundary, a small interface and no dependency on the
caller's environment:

```
modules/ecs-service/
  main.tf         # resources
  variables.tf    # inputs, with type, description and validation
  outputs.tf      # what the caller needs
  versions.tf     # required_version and required_providers
  README.md       # usage example
```

```hcl
# variables.tf
variable "service_name" {
  type        = string
  description = "Service name; composes the name of every resource"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,29}$", var.service_name))
    error_message = "service_name: lowercase, digits and hyphens, 3-30 characters."
  }
}

variable "desired_count" {
  type        = number
  description = "Number of running tasks"
  default     = 2

  validation {
    condition     = var.desired_count >= 1
    error_message = "desired_count must be at least 1."
  }
}
```

`validation` turns a configuration mistake into a clear message at `plan` time,
instead of an obscure cloud API error halfway through `apply` — when half the
resources already exist.

Rules that keep a module reusable:

- **A module declares no `provider` and no `backend`** — the caller configures those.
  A module with an embedded `provider` cannot be used in two regions.
- **No environment values baked in.** No `if var.env == "prod"` inside; the caller
  passes the value.
- **Do not abstract early.** A module used once is indirection with no benefit. Write
  it inline, extract on the second use — when you know what actually varies.
- **Registry or your own, not both for the same thing.** A community module saves time
  and costs flexibility; choose per case and do not mix.

## Naming and tagging

```hcl
locals {
  name_prefix = "${var.service_name}-${var.environment}"

  common_tags = {
    Service     = var.service_name
    Environment = var.environment
    ManagedBy   = "terraform"
    Repository  = var.repository_url    # where the code that created this lives
    CostCenter  = var.cost_center
  }
}

resource "aws_ecs_service" "this" {
  name = local.name_prefix
  tags = local.common_tags
}
```

`ManagedBy = "terraform"` answers the question that comes up in every incident: "can
I change this from the console?". `Repository` answers "where is the code for this?"
— and those two save hours when someone finds an orphaned resource.

Default tags across the account via the provider, so it does not depend on
remembering per resource:

```hcl
provider "aws" {
  region = var.region
  default_tags { tags = local.common_tags }
}
```

A module's primary resource is named `this`.
`resource "aws_ecs_service" "ecs_service"` is redundant — the type is already in the
address.

## `for_each`, not `count`

```hcl
# count: removing the middle item recreates ALL the following ones,
# because every index shifts
resource "aws_ssm_parameter" "config" {
  count = length(var.params)
  name  = var.params[count.index].name
}

# for_each: the address is the key; removing one does not touch the others
resource "aws_ssm_parameter" "config" {
  for_each = var.params            # map or set
  name     = each.key
  value    = each.value
}
```

Use `count` only for the on/off case: `count = var.enabled ? 1 : 0`.

This is the most common cause of "why does the plan want to recreate everything?" —
someone removed an item from the middle of a `count`-indexed list.

## Secrets

A secret **never** goes in a `.tf` or a committed `.tfvars`. And remember it ends up
in state regardless — which is why state is treated as a secret.

```hcl
# read from a secret manager, do not write
data "aws_secretsmanager_secret_version" "db" {
  secret_id = "prod/payments/db"
}

resource "aws_db_instance" "this" {
  password = jsondecode(data.aws_secretsmanager_secret_version.db.secret_string)["password"]
}
```

Better still: generate the password outside Terraform (or with RDS
`manage_master_user_password`, which delegates rotation to AWS) so the value never
passes through state at all.

Mandatory `.gitignore`:

```
*.tfstate
*.tfstate.*
.terraform/
*.tfvars              # except the example ones
!example.tfvars
crash.log
```

`.terraform.lock.hcl` **is committed**: it pins the provider version and hash, and it
is what guarantees CI and your machine use the exact same binary.

## The cycle

```bash
terraform init
terraform fmt -recursive -check      # fails if not formatted
terraform validate
terraform plan -out=tfplan           # ALWAYS save the plan
terraform apply tfplan               # apply the saved plan, do not replan
```

`apply` without a saved plan replans at apply time — and what you reviewed is not
necessarily what runs, if anything changed in between.

**Reading the plan is the review that matters.** Before approving, look for:

- **Unintended `destroy` or `replace`** — the biggest risk. A `-/+` on a database or a
  disk is the moment to stop.
- `(known after apply)` on a resource that should not change — usually an implicit
  dependency or drift.
- Counts: 3 resources changing when the change should affect 1.

```bash
terraform plan -out=tfplan && terraform show -json tfplan | \
  jq -r '.resource_changes[] | select(.change.actions[] | . == "delete") | .address'
```

That command lists everything that will be destroyed. It is worth making a mandatory
step in production CI: if the list is not empty, require human approval.

`prevent_destroy` on anything that must not disappear:

```hcl
resource "aws_db_instance" "this" {
  lifecycle { prevent_destroy = true }
}
```

## Drift

A change made in the console never returns to the code, and the next `apply` undoes
it — sometimes reverting what someone adjusted urgently during an incident.

```bash
terraform plan -detailed-exitcode     # 0 = no change, 2 = drift, 1 = error
```

Run that on a schedule in CI (daily) to detect it. When there is drift: bring the
change into the code, do not silently undo it — whoever made it had a reason, and
finding out what it was is part of the job.

## Per cloud

- `references/aws.md` — S3 backend, OIDC for CI, IAM, tagging, VPC, ECS/Lambda, RDS.
  **Read this when working with AWS.**
- `references/gcp.md` — equivalents and where GCP diverges.
- `references/azure.md` — equivalents and where Azure diverges.

## Checklist

- Remote state, encrypted, with locking; `*.tfstate` in `.gitignore`
- `.terraform.lock.hcl` committed
- One state per environment + change domain; no `workspace` for environments
- Provider version pinned at the minor
- `for_each` instead of `count` for collections
- Every variable has `type` and `description`; validation where format matters
- Default tags via the provider
- Secrets read from a manager, never written into `.tf`
- Plan saved, read and approved before `apply`
- `prevent_destroy` on databases, data buckets and anything else that must not vanish
