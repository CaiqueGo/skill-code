#!/usr/bin/env python3
"""Generate per-skill trigger-eval files from the shared corpus.

Usage:
    python evals/build_eval_sets.py

For each skill the eval file contains:
  - its own queries as positives
  - queries from the skills most likely to be confused with it, as hard negatives
  - unrelated queries as easy negatives

Drawing negatives from sibling skills is the point: these nine skills share a
domain, so the real failure mode is not "the skill never fires", it is "six skills
fire on the same prompt". A negative set of only unrelated questions would score
well and measure nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Which skills are genuinely confusable with which. Derived from overlapping
# vocabulary and overlapping moments of use, not from topic similarity alone.
CONFUSABLE_WITH: dict[str, list[str]] = {
    "fastapi-architecture": ["python-testing", "fastapi-async", "project-context-discovery"],
    "fastapi-async": ["fastapi-architecture", "python-testing", "cicd-pipelines"],
    "python-testing": ["fastapi-architecture", "cicd-pipelines", "pr-review"],
    "pr-review": ["code-security", "gitflow", "project-context-discovery"],
    "code-security": ["pr-review", "cicd-pipelines", "terraform-standards"],
    "gitflow": ["pr-review", "cicd-pipelines", "project-context-discovery"],
    "terraform-standards": ["cicd-pipelines", "code-security", "fastapi-architecture"],
    "cicd-pipelines": ["gitflow", "terraform-standards", "python-testing"],
    "project-context-discovery": ["fastapi-architecture", "pr-review", "python-testing"],
}

HARD_NEGATIVES_PER_SIBLING = 2
EASY_NEGATIVES = 4


def main() -> int:
    corpus = json.loads((HERE / "corpus.json").read_text(encoding="utf-8"))
    queries = corpus["queries"]

    by_skill: dict[str, list[str]] = {}
    for item in queries:
        by_skill.setdefault(item["skill"], []).append(item["query"])

    unrelated = by_skill.get("none", [])
    out_dir = HERE / "sets"
    out_dir.mkdir(exist_ok=True)

    summary: list[str] = []
    for skill, siblings in CONFUSABLE_WITH.items():
        positives = by_skill.get(skill, [])
        if not positives:
            print(f"warning: no queries tagged for {skill}")
            continue

        eval_items = [{"query": q, "should_trigger": True} for q in positives]

        for sibling in siblings:
            for q in by_skill.get(sibling, [])[:HARD_NEGATIVES_PER_SIBLING]:
                eval_items.append({"query": q, "should_trigger": False})

        for q in unrelated[:EASY_NEGATIVES]:
            eval_items.append({"query": q, "should_trigger": False})

        path = out_dir / f"{skill}.json"
        path.write_text(json.dumps(eval_items, indent=2, ensure_ascii=False), encoding="utf-8")

        n_pos = sum(1 for i in eval_items if i["should_trigger"])
        n_neg = len(eval_items) - n_pos
        summary.append(f"  {skill:28} {n_pos} positives, {n_neg} negatives")

    print(f"wrote {len(summary)} eval sets to {out_dir}")
    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
