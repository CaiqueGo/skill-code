# evals — does each skill fire when it should?

> **Running the full suite costs real money.** `run_trigger_eval.py` starts one
> `claude -p` session per query per run. A full run is currently 282 sessions and once consumed a
> large share of a personal token budget in under five minutes. The script refuses
> runs above 60 sessions unless you pass `--yes`. Start with the cheap command
> below and only scale up deliberately.

## What this measures, and what it does not

A unit test is deterministic. Skill triggering is not: when someone types *"this
endpoint is getting too big"*, the model **decides** whether to consult
`fastapi-architecture`, and that decision varies between runs. Measured here, the
same query fired on one run and not the next.

So the question is never "does it trigger?" but **"what fraction of the time?"** —
which needs realistic prompts, repeated runs, and a rate.

- **Measures**: the `description` field. Does the skill wake on the right prompts
  and stay quiet on the wrong ones.
- **Does not measure**: whether the skill's content is any good. That is your
  judgment, reading the output.

## Files

| File | Role |
|---|---|
| `corpus.json` | **The durable asset.** 94 realistic prompts, each labelled with the one skill that should fire (or `none`). Written the way people actually type — slang, typos, context, some in Portuguese |
| `build_eval_sets.py` → `sets/` | One file per skill. The trick: one skill's negatives are the other skills' positives, which is what tests cross-talk |
| `fixture.py` | Builds a small but realistic FastAPI project — git history, a pending diff, a CI workflow, Terraform — seeded with the exact defects the skills describe |
| `run_trigger_eval.py` | **The expensive one.** Installs every skill in a throwaway project and runs each prompt N times, recording which skills fired |
| `analyze_results.py` | Turns raw results into recall / precision / noise per skill |
| `results-*.json` | Measurements already taken — see below |

## Running it

```bash
# cheap: 28 queries x 2 runs = 56 sessions. Start here.
python evals/run_trigger_eval.py --limit 2 --runs 2 --workers 4

python evals/analyze_results.py evals/results.json
```

Never pass `--no-block` on a large run: unrestricted tools multiply the turns, and
therefore the tokens, in every session.

After changing any `description`, rerun the cheap command and compare recall and
precision against `results-fixture.json`. They pull in opposite directions — a
pushier description raises recall and lowers precision — so reading either alone
leads to the wrong edit.

## What has been measured so far

Two full runs (192 sessions each), 2026-09-14, before `concurrency-correctness`
existed:

| | `results-baseline.json` | `results-fixture.json` |
|---|---|---|
| Workspace | empty directory | realistic project (`fixture.py`) |
| Tools | `Read`/`Grep`/`Bash` blocked | only mutation and network blocked |
| Mean recall | ~5% | ~5% |
| False positives on unrelated prompts | 0 / 30 | 0 / 30 |
| Precision where it fired | 100% | 100% |

**A hypothesis that turned out wrong, recorded so nobody pays for it twice.** In
the first run, every skill whose prompts presuppose inspecting something —
`pr-review`, `code-security`, `cicd-pipelines`, `gitflow`,
`project-context-discovery` — scored 0%, while the purely conceptual ones scored
above zero. That looked conclusive: the workspace was empty, so "review this diff"
had no diff. `fixture.py` was written to fix exactly that.

It changed nothing. Recall stayed at ~5%. The environment was not the cause.

**What can be claimed:** the skills load and fire, precision is 100% where they
fire, and there is zero cross-talk — no skill fired on another's prompt, and
nothing fired on the ten unrelated prompts. That was the main risk of shipping ten
skills in one domain, and it is not happening.

**What cannot be claimed:** whether ~5% recall means the descriptions are weak, or
that headless `claude -p` consults skills rarely in general because the model
answers from its own knowledge. Separating those needs a control — the same corpus
against a skill with a known-good description — and that costs another full run.
It has not been done.

The cheap way to answer it is to use the skills in real work and notice whether
they show up when they should. That costs nothing and tests the real environment
rather than a headless one.

## Agents are not measured here

`run_trigger_eval.py` detects invocations of the `Skill` tool. An agent is reached
through a different path, so a skill that delegates — `project-context-discovery`
spawning `context-discovery` — still registers as that skill firing, while the
agent's own routing is untested. Judge agents by reading their reports.

## Adding a skill

Add its prompts to `corpus.json` tagged with its name — six or so, phrased
differently from each other, including at least one the way a person would ask it
without naming the topic. Then rerun `build_eval_sets.py`, which automatically
turns those prompts into negatives for every other skill. Skipping that step is how
cross-talk returns without anyone noticing.
