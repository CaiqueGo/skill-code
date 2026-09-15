#!/usr/bin/env python3
"""Scaffold a domain module across the manager-pattern layers.

Usage:
    python new_module.py invoice --root src
    python new_module.py payment_method --root src --dry-run

Creates entity, ports, errors, manager, repository, schemas, route and a unit test,
all wired with the correct cross-layer imports. The goal is that nobody has to
remember the structure or copy-paste from another module — which is how convention
drift starts.

Never overwrites an existing file: it reports and moves on.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Templates. `{snake}` = order, `{pascal}` = Order, `{plural}` = orders
# --------------------------------------------------------------------------- #

TEMPLATES: dict[str, str] = {
    "domain/{snake}.py": '''\
from dataclasses import dataclass
from enum import Enum
from typing import NewType

from domain.{snake}_errors import {pascal}AlreadyArchived

{pascal}Id = NewType("{pascal}Id", str)


class {pascal}Status(Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


@dataclass(slots=True)
class {pascal}:
    id: {pascal}Id
    status: {pascal}Status = {pascal}Status.ACTIVE

    def archive(self) -> None:
        """Object invariants live here, not in the manager."""
        if self.status is {pascal}Status.ARCHIVED:
            raise {pascal}AlreadyArchived(self.id)
        self.status = {pascal}Status.ARCHIVED
''',
    "domain/{snake}_errors.py": '''\
from domain.errors import ConflictError, NotFoundError


class {pascal}NotFound(NotFoundError):
    code = "{snake}_not_found"

    def __init__(self, {snake}_id: str) -> None:
        super().__init__(f"{snake} {{{snake}_id}} not found")
        self.{snake}_id = {snake}_id


class {pascal}AlreadyArchived(ConflictError):
    code = "{snake}_already_archived"

    def __init__(self, {snake}_id: str) -> None:
        super().__init__(f"{snake} {{{snake}_id}} is already archived")
        self.{snake}_id = {snake}_id
''',
    "domain/{snake}_ports.py": '''\
from typing import Protocol

from domain.{snake} import {pascal}, {pascal}Id


class {pascal}Repository(Protocol):
    async def get(self, {snake}_id: {pascal}Id) -> {pascal} | None: ...
    async def save(self, {snake}: {pascal}) -> None: ...
''',
    "managers/{snake}_manager.py": '''\
from domain.{snake} import {pascal}, {pascal}Id
from domain.{snake}_errors import {pascal}NotFound
from domain.{snake}_ports import {pascal}Repository


class {pascal}Manager:
    """Use cases for {snake}. No fastapi, no sqlalchemy — pure Python."""

    def __init__(self, {plural}: {pascal}Repository) -> None:
        self._{plural} = {plural}

    async def get(self, {snake}_id: {pascal}Id) -> {pascal}:
        {snake} = await self._{plural}.get({snake}_id)
        if {snake} is None:
            raise {pascal}NotFound({snake}_id)
        return {snake}

    async def archive(self, {snake}_id: {pascal}Id) -> {pascal}:
        {snake} = await self.get({snake}_id)
        {snake}.archive()
        await self._{plural}.save({snake})
        return {snake}
''',
    "repositories/{snake}_repository.py": '''\
from sqlalchemy.ext.asyncio import AsyncSession

from domain.{snake} import {pascal}, {pascal}Id


class Sql{pascal}Repository:
    """Satisfies {pascal}Repository structurally — it does not inherit the Protocol."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, {snake}_id: {pascal}Id) -> {pascal} | None:
        raise NotImplementedError

    async def save(self, {snake}: {pascal}) -> None:
        raise NotImplementedError
''',
    "api/schemas/{snake}.py": '''\
from pydantic import BaseModel, ConfigDict

from domain.{snake} import {pascal}Status


class {pascal}Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: {pascal}Status
''',
    "api/routes/{plural}.py": '''\
from fastapi import APIRouter, Depends

from api.deps import get_{snake}_manager
from api.schemas.{snake} import {pascal}Out
from domain.{snake} import {pascal}Id
from managers.{snake}_manager import {pascal}Manager

router = APIRouter(prefix="/{kebab}", tags=["{kebab}"])


@router.get("/{{{snake}_id}}", response_model={pascal}Out)
async def get_{snake}(
    {snake}_id: str,
    manager: {pascal}Manager = Depends(get_{snake}_manager),
) -> {pascal}Out:
    return {pascal}Out.model_validate(await manager.get({pascal}Id({snake}_id)))


@router.post("/{{{snake}_id}}/archive", response_model={pascal}Out)
async def archive_{snake}(
    {snake}_id: str,
    manager: {pascal}Manager = Depends(get_{snake}_manager),
) -> {pascal}Out:
    return {pascal}Out.model_validate(await manager.archive({pascal}Id({snake}_id)))
''',
    "tests/unit/test_{snake}_manager.py": '''\
import pytest

from domain.{snake} import {pascal}, {pascal}Id, {pascal}Status
from domain.{snake}_errors import {pascal}NotFound
from managers.{snake}_manager import {pascal}Manager


class Fake{pascal}Repository:
    def __init__(self, items: list[{pascal}] | None = None) -> None:
        self._by_id = {{i.id: i for i in items or []}}
        self.saved: list[{pascal}] = []

    async def get(self, {snake}_id: {pascal}Id) -> {pascal} | None:
        return self._by_id.get({snake}_id)

    async def save(self, {snake}: {pascal}) -> None:
        self.saved.append({snake})


async def test_archive_changes_status_and_persists() -> None:
    item = {pascal}(id={pascal}Id("x1"))
    repo = Fake{pascal}Repository([item])
    manager = {pascal}Manager({plural}=repo)

    result = await manager.archive({pascal}Id("x1"))

    assert result.status is {pascal}Status.ARCHIVED
    assert repo.saved == [result]


async def test_get_unknown_raises_not_found() -> None:
    manager = {pascal}Manager({plural}=Fake{pascal}Repository())

    with pytest.raises({pascal}NotFound):
        await manager.get({pascal}Id("nope"))
''',
}

# Irregular plurals common in domain names. Extend as your project needs.
IRREGULAR_PLURALS = {
    "person": "people",
    "child": "children",
    "datum": "data",
}


def pluralize(snake: str) -> str:
    """Pluralize the last word of the name (payment_method -> payment_methods)."""
    head, _, last = snake.rpartition("_")
    if last in IRREGULAR_PLURALS:
        plural_last = IRREGULAR_PLURALS[last]
    elif last.endswith("y") and not last.endswith(("ay", "ey", "iy", "oy", "uy")):
        plural_last = last[:-1] + "ies"
    elif last.endswith(("s", "x", "z", "ch", "sh")):
        plural_last = last + "es"
    else:
        plural_last = last + "s"
    return f"{head}_{plural_last}" if head else plural_last


def pascalize(snake: str) -> str:
    return "".join(part.capitalize() for part in snake.split("_"))


def render(template: str, snake: str) -> str:
    plural = pluralize(snake)
    return template.format(
        snake=snake,
        pascal=pascalize(snake),
        plural=plural,
        kebab=plural.replace("_", "-"),   # public URLs use hyphens, not underscores
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="domain name in snake_case, singular (e.g. invoice)")
    parser.add_argument("--root", default="src", help="source root directory (default: src)")
    parser.add_argument("--dry-run", action="store_true", help="only show what would be created")
    args = parser.parse_args()

    snake = args.name.strip().lower()
    if not snake.replace("_", "").isalnum() or snake[0].isdigit():
        print(f"error: '{args.name}' is not a valid snake_case identifier", file=sys.stderr)
        return 2

    root = Path(args.root)
    created: list[Path] = []
    skipped: list[Path] = []

    for path_template, body_template in TEMPLATES.items():
        rel = render(path_template, snake)
        # tests/ lives next to the source root, not inside it
        target = (root.parent / rel) if rel.startswith("tests/") else (root / rel)

        if target.exists():
            skipped.append(target)
            continue

        if not args.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render(body_template, snake), encoding="utf-8")
        created.append(target)

    prefix = "[dry-run] would create" if args.dry_run else "created"
    for p in created:
        print(f"{prefix}: {p}")
    for p in skipped:
        print(f"already exists, skipped: {p}")

    if created and not args.dry_run:
        print(
            f"\nStill to wire up:\n"
            f"  1. api/deps.py       -> get_{snake}_manager(session) building the manager\n"
            f"  2. main.py           -> app.include_router({pluralize(snake)}.router)\n"
            f"  3. repositories/     -> implement get/save (currently NotImplementedError)\n"
            f"  4. infra/models.py   -> the table, if this domain is persisted"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
