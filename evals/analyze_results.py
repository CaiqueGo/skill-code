#!/usr/bin/env python3
"""Summarize a trigger-eval run into the numbers that drive description edits.

Usage:
    python evals/analyze_results.py evals/results-baseline.json

Reports, per skill:
  recall    — of the queries that should fire it, how often it actually fired
  precision — of the times it fired, how often it was the right skill
  noise     — how often it fired on a query belonging to a different skill

Recall and precision pull in opposite directions, which is the whole tension in
writing a description: a pushier description raises recall and lowers precision.
Looking at either alone leads to the wrong edit.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    results = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))

    skills = sorted({r["expected"] for r in results if r["expected"] != "none"})
    fired_total: dict[str, float] = defaultdict(float)     # times fired, any query
    fired_correct: dict[str, float] = defaultdict(float)   # times fired on its own query
    expected_runs: dict[str, float] = defaultdict(float)   # runs where it should fire

    for r in results:
        n = r["runs"]
        expected = r["expected"]
        if expected != "none":
            expected_runs[expected] += n
        for skill, count in r["fired_counts"].items():
            fired_total[skill] += count
            if skill == expected:
                fired_correct[skill] += count

    print(f"{'skill':28} {'recall':>8} {'precision':>10} {'fired':>7} {'noise':>7}")
    print("-" * 64)
    for skill in skills:
        recall = fired_correct[skill] / expected_runs[skill] if expected_runs[skill] else 0.0
        precision = fired_correct[skill] / fired_total[skill] if fired_total[skill] else 0.0
        noise = fired_total[skill] - fired_correct[skill]
        print(f"{skill:28} {recall:>7.0%} {precision:>10.0%} "
              f"{fired_total[skill]:>7.0f} {noise:>7.0f}")

    # negatives: how often anything fired on an unrelated query
    none_rows = [r for r in results if r["expected"] == "none"]
    if none_rows:
        runs = sum(r["runs"] for r in none_rows)
        spurious = sum(sum(r["fired_counts"].values()) for r in none_rows)
        print(f"\nunrelated queries: {len(none_rows)} queries, {runs} runs, "
              f"{spurious} spurious triggers ({spurious / runs:.0%})")

    print("\nweakest recall — these under-trigger:")
    weak = sorted(
        ((fired_correct[s] / expected_runs[s] if expected_runs[s] else 0.0, s) for s in skills)
    )[:4]
    for rate, skill in weak:
        print(f"  {rate:>5.0%}  {skill}")
        for r in results:
            if r["expected"] == skill and (r["trigger_rate"] or 0) < 0.5:
                other = ", ".join(k for k in r["fired_counts"] if k != skill) or "nothing"
                print(f"          {r['trigger_rate']:.0%} | fired instead: {other}")
                print(f"                q: {r['query'][:78]}")

    print("\nnoisiest — these fire where they should not:")
    noisy = sorted(((fired_total[s] - fired_correct[s], s) for s in skills), reverse=True)[:4]
    for noise, skill in noisy:
        if noise == 0:
            continue
        print(f"  {noise:>3.0f} spurious  {skill}")
        for r in results:
            if r["expected"] != skill and skill in r["fired_counts"]:
                print(f"          on a {r['expected']} query: {r['query'][:66]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
