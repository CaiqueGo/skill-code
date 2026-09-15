# Terraform on Azure

A skeleton of equivalents. The general standards live in `SKILL.md`; only the
differences are here.

## Backend

```hcl
terraform {
  backend "azurerm" {
    resource_group_name  = "rg-tfstate"
    storage_account_name = "stmyorgtfstate"
    container_name       = "tfstate"
    key                  = "services/payments/prod.tfstate"
    use_azuread_auth     = true     # instead of the storage account access key
  }
}
```

**Locking is automatic** (blob lease) — nothing to configure. Enable versioning and
soft delete on the container.

`use_azuread_auth = true` avoids distributing the storage account access key, a
long-lived credential with full power over the state.

## The mental model that diverges most: the resource group

The **resource group** has no direct equivalent on AWS or GCP. It is a mandatory
container: every resource belongs to exactly one, and deleting the group deletes
everything inside it.

That is powerful and dangerous in equal measure. One resource group per environment
and per change domain, aligned with the state separation:

```hcl
resource "azurerm_resource_group" "this" {
  name     = "rg-${var.service_name}-${var.environment}"
  location = var.location
  tags     = local.common_tags
}
```

Storage account names are globally unique, lowercase and digits only, max 24
characters — Azure's most annoying naming constraint, and it breaks the prefix
pattern used everywhere else.

## CI authentication — OIDC

```hcl
resource "azuread_application" "github" {
  display_name = "github-${var.service_name}"
}

resource "azuread_application_federated_identity_credential" "main" {
  application_id = azuread_application.github.id
  display_name   = "github-main"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:my-org/my-repo:ref:refs/heads/main"
}
```

`subject` is exact — no wildcards. One federated credential per branch or per
environment, which in practice is safer than AWS's `StringLike`.

## Equivalents

| AWS | Azure | Note |
|---|---|---|
| Account | Subscription | billing and quota boundary |
| — | **Resource group** | mandatory; no AWS equivalent |
| Tags | Tags | the same |
| IAM role | Managed Identity | prefer *user-assigned* over *system-assigned* |
| IAM policy | Role assignment | built-in roles usually suffice |
| Secrets Manager | Key Vault | |
| ECS Fargate | Container Apps | closest equivalent |
| ALB | Application Gateway | |
| RDS | Azure Database for PostgreSQL | Flexible Server is the current one |
| CloudWatch | Azure Monitor / Log Analytics | |

## Managed Identity

```hcl
# user-assigned survives recreation of the resource that uses it —
# system-assigned disappears with it, and role assignments must be redone
resource "azurerm_user_assigned_identity" "app" {
  name                = "id-${local.name_prefix}"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
}

resource "azurerm_role_assignment" "kv" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}
```

Prefer *user-assigned*: with *system-assigned*, recreating the Container App produces
a new principal and every role assignment points at an identity that no longer
exists — a runtime failure, not an `apply` failure.

## Container Apps

```hcl
resource "azurerm_container_app" "this" {
  name                         = local.name_prefix
  resource_group_name          = azurerm_resource_group.this.name
  container_app_environment_id = var.environment_id
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  secret {
    name                = "database-url"
    key_vault_secret_id = azurerm_key_vault_secret.db.id
    identity            = azurerm_user_assigned_identity.app.id
  }

  template {
    min_replicas = var.environment == "prod" ? 1 : 0
    max_replicas = 10

    container {
      name   = var.service_name
      image  = var.image
      cpu    = 0.5
      memory = "1Gi"

      env {
        name        = "DATABASE_URL"
        secret_name = "database-url"
      }
    }
  }
}
```

## Key Vault — the detail that breaks `terraform destroy`

Key Vault has **mandatory soft delete** and it cannot be turned off. A destroyed
vault keeps holding its name for the retention period, and recreating it with the
same name fails until it is purged.

```hcl
resource "azurerm_key_vault" "this" {
  soft_delete_retention_days = 7      # the minimum; the default is 90
  purge_protection_enabled   = var.environment == "prod"
}
```

In ephemeral environments, leave `purge_protection_enabled = false` — with it on, not
even a manual purge is possible, and the name stays blocked for the full period.

## Validation

```bash
tflint --init     # azurerm plugin
checkov -d . --framework terraform
az deployment group what-if ...
```
