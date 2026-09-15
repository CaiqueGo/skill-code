#!/usr/bin/env python3
"""Build a realistic throwaway project for trigger evaluation.

The first baseline ran against an empty directory, and every skill whose queries
presuppose looking at something — a diff, a workflow, a lockfile, a repo — scored
0%. That was the environment failing, not the description: asked to "review this
diff" with no diff present, the model has nothing to review and answers from
general knowledge.

So the fixture provides what those queries refer to: a small FastAPI service with
layers, tests, a CI workflow, Terraform, a git history, and uncommitted changes in
the working tree.

Usage (standalone, for inspection):
    python evals/fixture.py /tmp/somewhere
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FILES: dict[str, str] = {
    "pyproject.toml": """\
[project]
name = "payments"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "sqlalchemy[asyncio]>=2.0",
    "asyncpg>=0.30",
    "pydantic-settings>=2.0",
    "httpx>=0.27",
    "requests>=2.32",
]

[dependency-groups]
dev = ["pytest>=8.0", "pytest-asyncio>=0.24", "ruff>=0.7", "mypy>=1.13"]

[tool.ruff.lint]
select = ["E", "F", "I"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
""",
    "README.md": """\
# payments

Internal payments service. FastAPI + Postgres, deployed to ECS.

    make test     run the suite
    make lint     ruff + mypy
""",
    "Makefile": """\
test:
\tpytest tests/

lint:
\truff check . && mypy src
""",
    "src/api/routes/orders.py": '''\
from fastapi import APIRouter, Depends

from src.api.deps import get_order_service
from src.services.order_service import OrderService

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/{order_id}")
async def get_order(order_id: str, service: OrderService = Depends(get_order_service)):
    order = await service.get(order_id)
    if order is None:
        return {"error": "not found"}
    return order
''',
    "src/api/deps.py": '''\
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.repositories.order_repository import OrderRepository
from src.services.order_service import OrderService
from src.infra.db import engine

session_factory = async_sessionmaker(engine)


async def get_session():
    async with session_factory() as session:
        yield session


def get_order_service(session: AsyncSession = None) -> OrderService:
    return OrderService(OrderRepository(session))
''',
    "src/services/order_service.py": '''\
import requests

from src.repositories.order_repository import OrderRepository


class OrderService:
    """Business layer. Note: this project calls it Service, not Manager."""

    def __init__(self, repository: OrderRepository) -> None:
        self.repository = repository

    async def get(self, order_id: str):
        return await self.repository.get(order_id)

    async def pay(self, order_id: str, amount_cents: int):
        order = await self.repository.get(order_id)
        if order is None:
            raise ValueError("order not found")
        if order["status"] == "paid":
            raise ValueError("already paid")

        # synchronous call inside an async method
        response = requests.post(
            "https://gateway.example.com/charge",
            json={"customer": order["customer_id"], "amount": amount_cents},
        )
        receipt = response.json()

        order["status"] = "paid"
        order["receipt_id"] = receipt["id"]
        await self.repository.save(order)
        return order
''',
    "src/repositories/order_repository.py": '''\
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, order_id: str):
        result = await self.session.execute(
            text(f"SELECT * FROM orders WHERE id = '{order_id}'")
        )
        row = result.first()
        return dict(row._mapping) if row else None

    async def save(self, order: dict) -> None:
        await self.session.execute(
            text("UPDATE orders SET status = :status WHERE id = :id"),
            {"status": order["status"], "id": order["id"]},
        )
        await self.session.commit()
''',
    "src/infra/db.py": '''\
import os

from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://localhost/payments")
# Deliberately a hardcoded default, so code-security has something to find.
# The value is an obvious placeholder rather than a real-looking key: a
# real-shaped one trips GitHub push protection, and teaching people to click
# "allow this secret" is the opposite of the point.
STRIPE_KEY = os.getenv("STRIPE_KEY", "sk_test_PLACEHOLDER_NOT_A_REAL_KEY")

engine = create_async_engine(DATABASE_URL, echo=False)
''',
    "tests/unit/test_order_service.py": '''\
from unittest import mock

import pytest

from src.services.order_service import OrderService


@pytest.fixture
def repository():
    return mock.AsyncMock()


async def test_get_returns_order(repository):
    repository.get.return_value = {"id": "o1", "status": "pending"}
    service = OrderService(repository)

    result = await service.get("o1")

    assert result["id"] == "o1"
''',
    ".github/workflows/ci.yml": """\
name: ci

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev]"
      - run: ruff check .
      - run: mypy src
      - run: pytest tests/ --cov=src
      - name: deploy
        if: github.ref == 'refs/heads/main'
        env:
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
        run: |
          aws ecr get-login-password | docker login --password-stdin $ECR
          docker build -t $ECR/payments:latest .
          docker push $ECR/payments:latest
          aws ecs update-service --cluster prod --service payments --force-new-deployment
""",
    "infra/main.tf": '''\
terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

variable "params" {
  type    = list(object({ name = string, value = string }))
  default = []
}

resource "aws_ssm_parameter" "config" {
  count = length(var.params)
  name  = var.params[count.index].name
  type  = "String"
  value = var.params[count.index].value
}

resource "aws_db_instance" "main" {
  identifier        = "payments"
  engine            = "postgres"
  instance_class    = "db.t3.medium"
  allocated_storage = 50
  password          = "hunter2"
}
''',
    "Dockerfile": """\
FROM python:3.12
WORKDIR /app
COPY . .
RUN pip install -e .
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0"]
""",
}

# Left uncommitted so `git diff` returns something for review-shaped queries.
WORKING_TREE_CHANGES: dict[str, str] = {
    "src/services/order_service.py": '''\
import requests

from src.repositories.order_repository import OrderRepository


class OrderService:
    """Business layer. Note: this project calls it Service, not Manager."""

    def __init__(self, repository: OrderRepository) -> None:
        self.repository = repository

    async def get(self, order_id: str):
        return await self.repository.get(order_id)

    async def pay(self, order_id: str, amount_cents: int):
        order = await self.repository.get(order_id)
        if order is None:
            raise ValueError("order not found")
        if order["status"] == "paid":
            raise ValueError("already paid")

        response = requests.post(
            "https://gateway.example.com/charge",
            json={"customer": order["customer_id"], "amount": amount_cents},
        )
        receipt = response.json()

        order["status"] = "paid"
        order["receipt_id"] = receipt["id"]
        await self.repository.save(order)
        return order

    async def compute_invoice_total(self, order_id: str) -> int:
        """Added: totals now include tax and the new handling fee."""
        order = await self.repository.get(order_id)
        subtotal = sum(item["price"] * item["qty"] for item in order["items"])
        tax = int(subtotal * 0.0825)
        handling = 499 if subtotal < 5000 else 0
        return subtotal + tax + handling
''',
}


def build(root: Path) -> None:
    for rel, content in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=root, check=False,
            capture_output=True, text=True,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "dev@example.com")
    git("config", "user.name", "dev")
    git("add", "-A")
    git("commit", "-qm", "feat(orders): add order lookup endpoint")

    # a little history, so `git log` is not a single commit
    (root / "src" / "api" / "routes" / "orders.py").write_text(
        FILES["src/api/routes/orders.py"] + '''

@router.post("/{order_id}/pay")
async def pay_order(order_id: str, amount_cents: int,
                    service: OrderService = Depends(get_order_service)):
    return await service.pay(order_id, amount_cents)
''',
        encoding="utf-8",
    )
    git("add", "-A")
    git("commit", "-qm", "feat(orders): add payment endpoint")

    # uncommitted change, so review-shaped queries have a real diff to look at
    for rel, content in WORKING_TREE_CHANGES.items():
        (root / rel).write_text(content, encoding="utf-8")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    build(root)
    print(f"built fixture project at {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
