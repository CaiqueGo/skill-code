#!/usr/bin/env python3
"""Validate the repository's skills before committing.

Usage:
    python scripts/validate_skills.py

Checks what breaks silently: missing or malformed frontmatter, a `name` that differs
from the directory (the skill never loads), a description too short to trigger well,
references to files that do not exist, and a SKILL.md too large for level 2 of
progressive disclosure.

Exits 1 on errors — wire it into CI.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SKILL_LINE_LIMIT = 500          # beyond this, move detail into references/
MIN_DESCRIPTION_WORDS = 25      # a short description triggers poorly

ROOT = Path(__file__).resolve().parent.parent


def parse_frontmatter(text: str) -> dict[str, str]:
    """Extract simple YAML frontmatter (single-line key: value pairs)."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}

    block = text[3:end]
    fields: dict[str, str] = {}
    current_key: str | None = None

    for line in block.splitlines():
        if not line.strip():
            continue
        m = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if m:
            current_key, value = m.group(1), m.group(2).strip()
            fields[current_key] = value
        elif current_key:                      # continuation of a multi-line value
            fields[current_key] += " " + line.strip()

    return fields


def validate_skill(skill_md: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    rel = skill_md.relative_to(ROOT).as_posix()
    text = skill_md.read_text(encoding="utf-8")

    fields = parse_frontmatter(text)
    if not fields:
        return [f"{rel}: missing or malformed frontmatter"], []

    dir_name = skill_md.parent.name
    name = fields.get("name", "")
    if not name:
        errors.append(f"{rel}: missing `name` in frontmatter")
    elif name != dir_name:
        errors.append(f"{rel}: name='{name}' differs from directory '{dir_name}' — will not load")

    description = fields.get("description", "")
    if not description:
        errors.append(f"{rel}: missing `description` — the skill will never trigger")
    else:
        word_count = len(description.split())
        if word_count < MIN_DESCRIPTION_WORDS:
            warnings.append(
                f"{rel}: description has {word_count} words; "
                f"also describe WHEN to trigger, not just what it does"
            )

    line_count = text.count("\n") + 1
    if line_count > SKILL_LINE_LIMIT:
        warnings.append(
            f"{rel}: {line_count} lines (ideal <= {SKILL_LINE_LIMIT}); "
            f"move detail into references/"
        )

    # files referenced from the body
    referenced = set(re.findall(r"`((?:references|scripts)/[\w\-./]+)`", text))
    for ref in sorted(referenced):
        if not (skill_md.parent / ref).exists():
            errors.append(f"{rel}: references `{ref}`, which does not exist")

    # files that exist but are never mentioned — likely orphans
    for sub in ("references", "scripts"):
        folder = skill_md.parent / sub
        if not folder.is_dir():
            continue
        for item in sorted(folder.iterdir()):
            if item.is_file() and f"{sub}/{item.name}" not in text:
                warnings.append(f"{rel}: {sub}/{item.name} exists but is not referenced in SKILL.md")

    return errors, warnings


def main() -> int:
    skills = sorted(ROOT.glob("plugins/*/skills/*/SKILL.md"))
    if not skills:
        print("no SKILL.md found under plugins/*/skills/*/", file=sys.stderr)
        return 1

    all_errors: list[str] = []
    all_warnings: list[str] = []
    for skill_md in skills:
        errors, warnings = validate_skill(skill_md)
        all_errors += errors
        all_warnings += warnings

    for warning in all_warnings:
        print(f"warning: {warning}")
    for error in all_errors:
        print(f"ERROR:   {error}", file=sys.stderr)

    print(f"\n{len(skills)} skills checked, {len(all_errors)} errors, {len(all_warnings)} warnings")
    return 1 if all_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
