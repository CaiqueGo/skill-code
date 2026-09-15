#!/usr/bin/env python3
"""Measure which skills actually trigger for each query in the corpus.

!!  COST WARNING — READ BEFORE RUNNING  !!

Every query starts a real `claude -p` session: full system prompt, every skill
description, plus however many turns the model takes. A full run is one session per
query per run — currently 70 queries x 3 runs = 210 sessions, and each one can read
files and run git commands against the fixture.

A full run measured here consumed a substantial share of a personal token budget
in under five minutes. It is not a unit test. Do not run it casually, do not run
it in CI, and do not leave it unattended.

Cheap version that gives most of the same signal:

    python evals/run_trigger_eval.py --limit 2 --runs 2 --workers 4   # 36 sessions

Never pass --no-block on a full run: unrestricted tools multiply the turns, and
therefore the tokens, in every single session.

Usage:
    python evals/run_trigger_eval.py                       # full corpus, 3 runs each
    python evals/run_trigger_eval.py --limit 1 --runs 1    # smoke test
    python evals/run_trigger_eval.py --runs 5 --workers 8

Why this exists instead of the skill-creator's runner: that one uses
`select.select()` on a subprocess pipe, which on Windows only works with sockets.
It fails silently and reports every query as "did not trigger" — a green-looking
run that measures nothing.

Two design choices that matter:

1. Every skill is installed into ONE throwaway project and each query records
   *which* skills fired. These skills share a domain, so the real failure is not
   "it never fires", it is "six fire at once". Testing one skill at a time cannot
   see that, and costs one full pass per skill.

2. Every query runs several times. Triggering is stochastic — measured here, the
   same query fired on one run and not on the next — so a single run per query
   produces numbers that look precise and are noise.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixture  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# Only mutation and network are blocked. Reading is left enabled on purpose:
# an earlier baseline blocked Read/Grep/Bash and every skill whose queries
# presuppose inspecting something scored 0%, which measured the restriction
# rather than the description.
BLOCKED_TOOLS = "Edit Write NotebookEdit WebFetch WebSearch Task"

# Above this many sessions the run needs --yes. Each session is a full
# `claude -p` invocation, so the cost scales with this number and nothing
# in the output warns you while it burns.
CONFIRM_ABOVE = 60


def claude_command() -> list[str]:
    """Resolve how to invoke the CLI as a subprocess.

    On Windows `claude` is `claude.cmd`, which Popen cannot exec from a list —
    it fails with WinError 2. Going through `cmd /c` would work but mangles the
    quoting of queries containing braces and quotes, so call the Node entrypoint
    directly instead.
    """
    launcher = shutil.which("claude")
    if launcher is None:
        sys.exit("error: `claude` not found on PATH")

    cli_js = Path(launcher).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    if cli_js.is_file():
        node = shutil.which("node")
        if node:
            return [node, str(cli_js)]

    if launcher.lower().endswith((".cmd", ".bat")):
        sys.exit(f"error: could not find cli.js next to {launcher}")
    return [launcher]


def install_skills(workspace: Path) -> list[str]:
    """Copy every skill in the repo into workspace/.claude/skills/."""
    target = workspace / ".claude" / "skills"
    target.mkdir(parents=True, exist_ok=True)

    names: list[str] = []
    for skill_md in sorted(REPO.glob("plugins/*/skills/*/SKILL.md")):
        src = skill_md.parent
        shutil.copytree(src, target / src.name, dirs_exist_ok=True)
        names.append(src.name)

    # a realistic project, so queries that refer to a diff / workflow / repo
    # have something to actually refer to
    fixture.build(workspace)
    return names


def one_run(
    query: str, workspace: Path, timeout: int, launcher: list[str], block_tools: bool
) -> tuple[set[str], str]:
    """Run one query headlessly; return the set of skills invoked."""
    cmd = [*launcher, "-p", query, "--output-format", "stream-json", "--verbose"]
    if block_tools:
        cmd += ["--disallowedTools", BLOCKED_TOOLS]

    # CLAUDECODE guards against interactive nesting; a subprocess call is fine.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    try:
        proc = subprocess.run(
            cmd, cwd=workspace, env=env, capture_output=True,
            text=True, timeout=timeout, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return set(), "timeout"
    except OSError as exc:
        return set(), f"oserror: {exc}"

    if proc.returncode != 0 and not proc.stdout:
        return set(), f"exit {proc.returncode}: {(proc.stderr or '')[:160]}"

    fired: set[str] = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        for block in (event.get("message") or {}).get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                if block.get("name") == "Skill":
                    name = (block.get("input") or {}).get("skill")
                    if name:
                        fired.add(str(name).split(":")[-1])

    return fired, "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="N queries per skill (0 = all)")
    parser.add_argument("--runs", type=int, default=3, help="runs per query (default 3)")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--out", default=str(HERE / "results.json"))
    parser.add_argument("--no-block", action="store_true",
                        help="do not restrict tools (far more tokens per session)")
    parser.add_argument("--yes", action="store_true",
                        help=f"confirm a run larger than {CONFIRM_ABOVE} sessions")
    args = parser.parse_args()

    corpus = json.loads((HERE / "corpus.json").read_text(encoding="utf-8"))
    queries = corpus["queries"]
    if args.limit:
        by_skill: dict[str, list[dict]] = defaultdict(list)
        for item in queries:
            by_skill[item["skill"]].append(item)
        queries = [
            items[i] for i in range(args.limit)
            for items in by_skill.values() if i < len(items)
        ]

    total_planned = len(queries) * args.runs
    if total_planned > CONFIRM_ABOVE and not args.yes:
        sys.exit(
            "\n".join([
                f"refusing to start {total_planned} claude sessions without --yes.",
                "each one is a full `claude -p` invocation; a run this size can",
                "consume a large share of a token budget in minutes.",
                "",
                "cheap alternative:",
                "  python evals/run_trigger_eval.py --limit 2 --runs 2 --workers 4",
                "",
                "if you really mean it, re-run with --yes.",
            ])
        )

    workspace = Path(tempfile.mkdtemp(prefix="skill-eval-"))
    try:
        launcher = claude_command()
        installed = install_skills(workspace)
        total = len(queries) * args.runs
        print(f"installed {len(installed)} skills into {workspace}", file=sys.stderr)
        print(f"{len(queries)} queries x {args.runs} runs = {total} invocations, "
              f"{args.workers} workers", file=sys.stderr)

        # flatten to (query_index, run_index) so every run is an independent unit
        jobs = [(qi, ri) for qi in range(len(queries)) for ri in range(args.runs)]
        fired_per_query: dict[int, list[set[str]]] = defaultdict(list)
        status_per_query: dict[int, list[str]] = defaultdict(list)
        done_count = 0

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(
                    one_run, queries[qi]["query"], workspace, args.timeout,
                    launcher, not args.no_block,
                ): qi
                for qi, _ in jobs
            }
            for future in as_completed(futures):
                qi = futures[future]
                fired, status = future.result()
                fired_per_query[qi].append({f for f in fired if f in installed})
                status_per_query[qi].append(status)
                done_count += 1
                if done_count % 20 == 0:
                    print(f"  ... {done_count}/{total}", file=sys.stderr)

        results = []
        for qi, item in enumerate(queries):
            runs = fired_per_query[qi]
            expected = item["skill"]
            counts = Counter(s for run in runs for s in run)
            n = len(runs) or 1

            results.append({
                "query": item["query"],
                "expected": expected,
                "runs": n,
                "trigger_rate": (counts.get(expected, 0) / n) if expected != "none" else None,
                "silent_rate": sum(1 for r in runs if not r) / n,
                "fired_counts": dict(counts.most_common()),
                "statuses": sorted(set(status_per_query[qi])),
            })

        Path(args.out).write_text(
            json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nwrote {args.out}", file=sys.stderr)
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
