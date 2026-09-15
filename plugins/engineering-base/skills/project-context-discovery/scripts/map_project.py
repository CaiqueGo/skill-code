#!/usr/bin/env python3
"""Collect a repository's deterministic facts in a single pass.

Usage:
    python map_project.py                 # current directory
    python map_project.py /path/to/repo
    python map_project.py --depth 4

Answers, without opening files one by one: what the stack is, which package manager
is used, which lint/type/test tools are configured, how the code is laid out, what
CI enforces, and which files the team actually touches.

Standard library only — runs anywhere with no install. It does not replace reading
a vertical slice of code; it replaces hunting for facts already written down in
configuration files.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

IGNORED = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".terraform", ".idea",
    ".vscode", "target", "vendor", ".next", "coverage", "htmlcov", ".tox",
}

# declarative file -> (stack label, package manager label)
STACK_MARKERS = {
    "pyproject.toml": ("Python", None),
    "requirements.txt": ("Python", "pip"),
    "setup.py": ("Python", "setuptools"),
    "Pipfile": ("Python", "pipenv"),
    "uv.lock": (None, "uv"),
    "poetry.lock": (None, "poetry"),
    "package.json": ("Node", None),
    "pnpm-lock.yaml": (None, "pnpm"),
    "yarn.lock": (None, "yarn"),
    "package-lock.json": (None, "npm"),
    "go.mod": ("Go", "go modules"),
    "Cargo.toml": ("Rust", "cargo"),
    "pom.xml": ("Java", "maven"),
    "build.gradle": ("Java", "gradle"),
    "build.gradle.kts": ("Kotlin", "gradle"),
    "Gemfile": ("Ruby", "bundler"),
    "composer.json": ("PHP", "composer"),
}

TASK_RUNNERS = ["Makefile", "justfile", "Justfile", "Taskfile.yml", "taskfile.yml"]

TOOL_CONFIGS = {
    "ruff.toml": "ruff", ".ruff.toml": "ruff",
    "setup.cfg": "flake8/setuptools", "tox.ini": "tox",
    ".pre-commit-config.yaml": "pre-commit",
    "mypy.ini": "mypy", ".mypy.ini": "mypy",
    ".eslintrc": "eslint", ".eslintrc.json": "eslint", "eslint.config.js": "eslint",
    ".prettierrc": "prettier", "biome.json": "biome",
    ".golangci.yml": "golangci-lint", ".golangci.yaml": "golangci-lint",
    "Dockerfile": "docker", "docker-compose.yml": "docker compose",
    "docker-compose.yaml": "docker compose",
    "alembic.ini": "alembic (migrations)",
}

INSTRUCTION_FILES = ["CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md", ".cursorrules"]

# tools that, when present in a CI step, act as quality gates
CI_GATES = [
    "ruff", "black", "flake8", "isort", "mypy", "pyright", "pytest", "coverage",
    "bandit", "pip-audit", "safety", "osv-scanner", "trivy", "semgrep", "snyk",
    "import-linter", "lint-imports", "eslint", "prettier", "vitest", "jest",
    "golangci-lint", "go test", "terraform", "tflint", "checkov", "tfsec",
]


def run(cmd: list[str], cwd: Path) -> str | None:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def detect_stack(root: Path) -> tuple[list[str], list[str]]:
    stacks: list[str] = []
    managers: list[str] = []
    for filename, (stack, manager) in STACK_MARKERS.items():
        if (root / filename).exists():
            if stack and stack not in stacks:
                stacks.append(stack)
            if manager and manager not in managers:
                managers.append(manager)

    # pyproject.toml with no lockfile: the poetry section disambiguates
    if not managers and (root / "pyproject.toml").exists():
        text = (root / "pyproject.toml").read_text(encoding="utf-8", errors="ignore")
        managers.append("poetry" if "[tool.poetry]" in text else "pyproject (pip/uv)")

    return stacks, managers


def read_pyproject(root: Path) -> dict[str, object]:
    path = root / "pyproject.toml"
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError):
        return {}

    project = data.get("project", {})
    tool = data.get("tool", {})
    deps = list(project.get("dependencies", []))
    # poetry keeps them elsewhere
    deps += list(tool.get("poetry", {}).get("dependencies", {}).keys())

    def package_name(spec: str) -> str:
        return re.split(r"[\[<>=!~ ;]", spec, maxsplit=1)[0].strip().lower()

    return {
        "requires_python": project.get("requires-python"),
        "dependencies": sorted({package_name(d) for d in deps if package_name(d)}),
        "configured_tools": sorted(tool.keys()),
        "mypy_strict": bool(tool.get("mypy", {}).get("strict")),
        "ruff_select": tool.get("ruff", {}).get("lint", {}).get("select")
        or tool.get("ruff", {}).get("select"),
        "asyncio_mode": tool.get("pytest", {}).get("ini_options", {}).get("asyncio_mode"),
        "has_import_linter_contract": "importlinter" in tool,
    }


def tree(root: Path, depth: int) -> list[str]:
    lines: list[str] = []

    def walk(d: Path, level: int, prefix: str) -> None:
        if level > depth:
            return
        try:
            children = sorted(
                p for p in d.iterdir() if p.is_dir() and p.name not in IGNORED
                and not p.name.startswith(".")
            )
        except OSError:
            return
        for child in children:
            try:
                n_files = sum(1 for p in child.iterdir() if p.is_file())
            except OSError:
                n_files = 0
            plural = "file" if n_files == 1 else "files"
            suffix = f"  ({n_files} {plural})" if n_files else ""
            lines.append(f"{prefix}{child.name}/{suffix}")
            walk(child, level + 1, prefix + "  ")

    walk(root, 1, "")
    return lines


def ci_gates(root: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    candidates: list[Path] = []
    wf = root / ".github" / "workflows"
    if wf.is_dir():
        candidates += sorted(p for p in wf.iterdir() if p.suffix in {".yml", ".yaml"})
    for name in (".gitlab-ci.yml", "azure-pipelines.yml", "Jenkinsfile", ".pre-commit-config.yaml"):
        if (root / name).exists():
            candidates.append(root / name)

    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        found = [g for g in CI_GATES if g in text]
        if found:
            result[str(path.relative_to(root)).replace("\\", "/")] = found
    return result


def hot_files(root: Path, n: int = 15) -> list[tuple[int, str]]:
    output = run(["git", "log", "--pretty=format:", "--name-only", "-80"], root)
    if not output:
        return []
    counts: dict[str, int] = {}
    for line in output.splitlines():
        path = line.strip()
        if not path or any(part in IGNORED for part in path.split("/")):
            continue
        counts[path] = counts.get(path, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    return [(c, p) for p, c in ordered[:n]]


def collect(root: Path, depth: int) -> dict[str, object]:
    stacks, managers = detect_stack(root)

    def present(names: list[str]) -> list[str]:
        return [n for n in names if (root / n).exists()]

    return {
        "root": str(root),
        "stack": stacks or ["unidentified"],
        "package_manager": managers or ["unidentified"],
        "task_runner": present(TASK_RUNNERS),
        "instruction_files": present(INSTRUCTION_FILES),
        "tools": sorted(
            {label for name, label in TOOL_CONFIGS.items() if (root / name).exists()}
        ),
        "python": read_pyproject(root),
        "layout": tree(root, depth),
        "ci_gates": ci_gates(root),
        "most_changed_files": hot_files(root),
        "current_branch": (run(["git", "branch", "--show-current"], root) or "").strip() or None,
    }


def render(d: dict[str, object]) -> None:
    def section(title: str) -> None:
        print(f"\n## {title}")

    print(f"# Map of {d['root']}")

    section("Stack")
    print(f"language: {', '.join(d['stack'])}")          # type: ignore[arg-type]
    print(f"packages: {', '.join(d['package_manager'])}")  # type: ignore[arg-type]
    if d["task_runner"]:
        print(f"tasks:    {', '.join(d['task_runner'])}  <- read before inventing a command")  # type: ignore[arg-type]
    if d["tools"]:
        print(f"tooling:  {', '.join(d['tools'])}")      # type: ignore[arg-type]

    py = d["python"]
    if isinstance(py, dict) and py:
        section("Python")
        if py.get("requires_python"):
            print(f"version:      {py['requires_python']}")
        if py.get("ruff_select"):
            print(f"ruff select:  {py['ruff_select']}")
        print(f"mypy strict:  {py.get('mypy_strict')}")
        if py.get("asyncio_mode"):
            print(f"asyncio_mode: {py['asyncio_mode']}")
        if py.get("has_import_linter_contract"):
            print("import-linter: configured (layers are enforced in CI)")
        deps = py.get("dependencies") or []
        if deps:
            print(f"dependencies ({len(deps)}): {', '.join(deps[:25])}")

    if d["instruction_files"]:
        section("Explicit instructions — these take precedence over inference")
        for name in d["instruction_files"]:      # type: ignore[union-attr]
            print(f"  READ: {name}")

    section("Layout")
    for line in d["layout"]:                     # type: ignore[union-attr]
        print(f"  {line}")

    gates = d["ci_gates"]
    if isinstance(gates, dict) and gates:
        section("CI gates — mandatory, not optional")
        for filename, tools in gates.items():
            print(f"  {filename}: {', '.join(tools)}")

    hot = d["most_changed_files"]
    if isinstance(hot, list) and hot:
        section("Live code — read a slice from here, not from the oldest module")
        for count, path in hot:
            print(f"  {count:>3}x  {path}")

    if d["current_branch"]:
        section("Git")
        print(f"current branch: {d['current_branch']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=".", help="repository root")
    parser.add_argument("--depth", type=int, default=3, help="tree levels (default: 3)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    data = collect(root, args.depth)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        render(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
