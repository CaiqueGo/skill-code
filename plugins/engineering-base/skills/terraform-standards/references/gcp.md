# Terraform on GCP

A skeleton of equivalents. The general standards (modules, `for_each`, tagging, the
plan/apply cycle) live in `SKILL.md` and apply unchanged — only the differences are
here.

## Backend

```hcl
terraform {
  backend "gcs" {
    bucket = "my-org-tfstate"
    prefix = "services/payments/prod"
  }
}
```

**Locking is automatic on GCS** — there is no DynamoDB-table equivalent to configure.
Enable object versioning on the bucket so a corrupted state can be recovered.

## The mental model that diverges most: the project

On AWS the account is a heavyweight boundary and most teams keep few of them. On GCP
the **project** is the natural unit of isolation — it is cheap to create, and the
common practice is one project per environment (`my-org-payments-prod`, `-staging`).

That replaces much of what AWS does with name prefixes and environment tags: IAM,
quota, billing and network isolation already come from the project.

```hcl
provider "google" {
  project = var.project_id
  region  = var.region
}
```

## CI authentication — Workload Identity Federation

The equivalent of AWS OIDC, and equally preferable to a JSON service account key
(a long-lived secret that leaks often):

```hcl
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github-pool"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-provider"

  oidc { issuer_uri = "https://token.actions.githubusercontent.com" }

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }

  # without this condition, any GitHub repository can obtain a token
  attribute_condition = "assertion.repository == 'my-org/my-repo'"
}
```

`attribute_condition` is the direct analogue of the AWS `sub` condition, and omitting
it is the same serious mistake.

## Equivalents

| AWS | GCP | Note |
|---|---|---|
| Account | Project | much lighter boundary; use one per environment |
| Tags | Labels | lowercase, hyphens; stricter character rules |
| Inline IAM policy | IAM binding on a resource | careful: `_binding` is **authoritative** |
| Secrets Manager | Secret Manager | `google_secret_manager_secret_version` |
| ECS Fargate | Cloud Run | Cloud Run is far simpler for HTTP |
| ALB | Cloud Load Balancing | more pieces (forwarding rule, proxy, backend) |
| RDS | Cloud SQL | `deletion_protection` works the same |
| VPC + per-AZ subnets | Global VPC, regional subnet | the GCP VPC is global, not regional |
| CloudWatch | Cloud Logging/Monitoring | |

## The IAM trap

This one causes real incidents and has no AWS equivalent:

```hcl
# AUTHORITATIVE: removes every other member holding that role on the project
resource "google_project_iam_binding" "editor" {
  project = var.project_id
  role    = "roles/editor"
  members = ["serviceAccount:${google_service_account.app.email}"]
}

# ADDITIVE: adds only this member — almost always what you want
resource "google_project_iam_member" "editor" {
  project = var.project_id
  role    = "roles/editor"
  member  = "serviceAccount:${google_service_account.app.email}"
}
```

`_binding` on a broad role revokes access for everyone not in the list — including
people. Use `_member` unless that is exactly the intent.

## Cloud Run — the shortest path to an HTTP service

```hcl
resource "google_cloud_run_v2_service" "this" {
  name     = local.name_prefix
  location = var.region

  template {
    service_account = google_service_account.app.email

    scaling {
      min_instance_count = var.environment == "prod" ? 1 : 0
      max_instance_count = 10
    }

    containers {
      image = var.image

      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db.secret_id
            version = "latest"
          }
        }
      }
    }
  }
}
```

`min_instance_count = 0` in non-production: it scales to zero and costs nothing idle.
In production, a minimum of 1 avoids cold-start latency on the first request.

## Validation

```bash
tflint --init     # google plugin
checkov -d . --framework terraform
gcloud recommender ...   # over-permissive IAM recommendations
```
