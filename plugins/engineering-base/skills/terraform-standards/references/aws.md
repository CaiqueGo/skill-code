# Terraform on AWS

## Backend

```hcl
terraform {
  backend "s3" {
    bucket       = "my-org-tfstate"
    key          = "services/payments/prod/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true      # S3 object lock; no DynamoDB needed (provider v6+)
    kms_key_id   = "alias/tfstate"
  }
}
```

On provider v5 or earlier, locking still requires a `dynamodb_table` with a `LockID`
string partition key.

The state bucket is created **outside** the Terraform that uses it — manual
bootstrap, or a separate Terraform whose local state is committed only in that case.
Configure on it: object versioning (so a corrupted state can be recovered), public
access block, default encryption, and a policy denying `s3:DeleteObject` to
non-administrators.

## CI authentication — OIDC, no static keys

An IAM user access key in a GitHub secret is the legacy pattern and the most common
source of leaked credentials. OIDC replaces it with short-lived credentials:

```hcl
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "github_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # restrict to the repository AND the branch — without this, any repo can assume it
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:my-org/my-repo:ref:refs/heads/main"]
    }
  }
}
```

The `sub` condition is what separates "authenticated CI" from "anyone on GitHub". An
overly broad `values = ["repo:my-org/*"]` hands the role to any repo in the
organization, including a malicious fork with a modified workflow.

Workflow usage is in `cicd-pipelines`.

## Provider and default tags

```hcl
provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Service     = var.service_name
      Environment = var.environment
      ManagedBy   = "terraform"
      Repository  = var.repository_url
    }
  }
}

# a second region when needed (e.g. ACM for CloudFront must be us-east-1)
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}
```

## IAM — the most expensive mistake

A policy with `Action = "*"` and `Resource = "*"` is the pattern that slips through
review because "it works". Write it with `aws_iam_policy_document`, which validates
the syntax at `plan` time:

```hcl
data "aws_iam_policy_document" "task" {
  statement {
    sid     = "ReadServiceSecret"
    actions = ["secretsmanager:GetSecretValue"]

    resources = [
      "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:${var.environment}/${var.service_name}/*"
    ]
  }

  statement {
    sid       = "PublishToQueue"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.events.arn]
  }
}
```

Two distinct roles per ECS service, and confusing them is common:
`execution_role_arn` is used **by the ECS agent** to pull the image and read secrets
at startup; `task_role_arn` is the identity **of your running code**. Application
permissions belong on the second one.

## Networking

```hcl
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = local.name_prefix
  cidr = var.vpc_cidr

  azs             = slice(data.aws_availability_zones.available.names, 0, 3)
  private_subnets = [for i in range(3) : cidrsubnet(var.vpc_cidr, 4, i)]
  public_subnets  = [for i in range(3) : cidrsubnet(var.vpc_cidr, 4, i + 8)]

  enable_nat_gateway = true
  single_nat_gateway = var.environment != "prod"   # 1 NAT in dev, 3 in prod
}
```

The community VPC module is one of the clearest cases where using the registry pays
off — a VPC has many coupled pieces and little real variation between projects.

`single_nat_gateway` is the most immediate cost optimization in development: NAT
Gateway bills per hour **and** per GB, and three of them in dev is money thrown away.

Security groups: reference the source group, not an open CIDR.

```hcl
resource "aws_vpc_security_group_ingress_rule" "from_alb" {
  security_group_id            = aws_security_group.service.id
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 8000
  to_port                      = 8000
  ip_protocol                  = "tcp"
}
```

## ECS Fargate — the parts usually missing

```hcl
resource "aws_ecs_service" "this" {
  name            = local.name_prefix
  cluster         = var.cluster_arn
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  # without this, the deploy drains the old tasks before the new ones are ready
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true      # rolls back on its own when the new task never becomes healthy
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.this.arn
    container_name   = var.service_name
    container_port   = 8000
  }

  lifecycle {
    # the pipeline updates the image; Terraform should not revert it
    ignore_changes = [task_definition]
  }
}
```

`deployment_circuit_breaker` with `rollback` is what turns a bad deploy into a
non-event. Without it, an image that fails to start leaves the service in a restart
loop until someone notices.

`ignore_changes = [task_definition]` settles the classic fight: the deploy pipeline
registers a new task definition revision, and the next `apply` would try to revert to
the revision in the code. With it, infrastructure and application deploys stop
fighting each other.

Secrets go by reference, never as literal environment variables:

```hcl
secrets = [{
  name      = "DATABASE_URL"
  valueFrom = "arn:aws:secretsmanager:...:secret:prod/payments/db-AbCdEf"
}]
```

## RDS

```hcl
resource "aws_db_instance" "this" {
  identifier     = local.name_prefix
  engine         = "postgres"
  engine_version = "16.4"
  instance_class = var.instance_class

  storage_encrypted     = true
  allocated_storage     = 50
  max_allocated_storage = 200          # storage autoscaling

  # let AWS generate and rotate it; the password never passes through state
  manage_master_user_password = true

  backup_retention_period = var.environment == "prod" ? 30 : 7
  deletion_protection     = var.environment == "prod"
  skip_final_snapshot     = var.environment != "prod"

  performance_insights_enabled = true

  lifecycle { prevent_destroy = true }
}
```

`manage_master_user_password` is the best practice here precisely because state
stores everything in plain text — what never enters state cannot leak through it.

## Cost

- `single_nat_gateway` outside production
- CloudWatch log retention (`retention_in_days`) — the default is forever, and
  forgotten logs are one of the fastest-growing lines on the bill
- ECR lifecycle policy: old images accumulate indefinitely
- Fargate Spot in non-production environments

## Static validation

```bash
tflint --init && tflint            # AWS-specific errors (nonexistent instance type, etc.)
checkov -d . --framework terraform # security policy
trivy config .                     # misconfiguration
```

`tflint` with the AWS plugin catches at `plan` time things that would otherwise only
surface during `apply`, like an instance type that does not exist in the chosen
region.
