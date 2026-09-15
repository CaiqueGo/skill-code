# Dependency scanners by ecosystem

Concrete commands. The rule that holds everywhere: **scan the lockfile or the
resolved environment, never just the manifest** — the CVE is almost always in a
transitive dependency the manifest never mentions.

## Python

```bash
uv tool install pip-audit            # or: pipx install pip-audit

pip-audit                            # current, resolved environment
pip-audit -r requirements.txt        # pinned file (the .txt, not the .in)
pip-audit --strict                   # non-zero exit on findings → use in CI
pip-audit --fix --dry-run            # shows the minimal upgrade that resolves it
```

With `uv` or `poetry`, export the resolved set before auditing — auditing the
manifest misses transitives:

```bash
uv export --no-hashes --format requirements-txt | pip-audit -r /dev/stdin
poetry export --without-hashes -f requirements.txt | pip-audit -r /dev/stdin
```

Complements with a distinct role:

```bash
ruff check --select S .              # bandit built in: flags YOUR code, not libraries
bandit -r src/ -ll                   # same, if the project does not use ruff yet
```

`pip-audit` looks at dependencies; `ruff --select S` looks at your code. They are
different problems and both need to run.

## Multi-ecosystem — `osv-scanner`

Most useful when the repository has more than one language, or Terraform and
containers alongside the application:

```bash
# install: https://github.com/google/osv-scanner/releases  (single binary)

osv-scanner scan source -r .              # finds every lockfile recursively
osv-scanner scan image my-image:tag       # container layers
osv-scanner scan source --lockfile=uv.lock .
osv-scanner --format markdown scan source -r .   # output for a PR comment
```

Recognized lockfiles: `uv.lock`, `poetry.lock`, `requirements.txt`, `Pipfile.lock`,
`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `go.mod`, `Cargo.lock`,
`pom.xml`, `gradle.lockfile`, `composer.lock`, `Gemfile.lock`.

Ignore with justification in `osv-scanner.toml`:

```toml
[[IgnoredVulns]]
id = "GHSA-xxxx-yyyy-zzzz"
ignoreUntil = 2026-12-14
reason = "XML parser unreachable; we only call to_json()"
```

## Node

```bash
npm audit --omit=dev                 # production; dev-only is rarely urgent
npm audit fix                        # patch/minor only
npm audit --audit-level=high         # non-zero exit from high upward

pnpm audit --prod
yarn npm audit --environment production
```

`npm audit` tends to be noisy about build-tool transitives. Cross-check with
`osv-scanner` before treating anything as urgent: different database, less noise.

## Go

```bash
govulncheck ./...                    # official, from the Go team
```

It is the best scanner in any ecosystem on this front: it performs **reachability
analysis** — it only reports CVEs whose vulnerable code is actually called by your
binary. It answers triage question 1 on its own.

## Containers and IaC

```bash
trivy image my-image:tag             # OS + application libraries, by layer
trivy fs .                           # filesystem
trivy config .                       # misconfiguration in Terraform/K8s/Dockerfile

checkov -d .                         # IaC policy
tfsec .                              # Terraform-specific
```

An outdated base image is a silent source of critical CVEs: a six-month-old
`python:3.12` carries dozens of OS-level flaws unrelated to your code. Pin the base
by digest and refresh it on a cadence.

## In CI (GitHub Actions)

```yaml
name: security
on:
  pull_request:
  schedule:
    - cron: "0 6 * * 1"        # Monday morning: catches CVEs published after merge

jobs:
  deps:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv export --no-hashes --format requirements-txt > req.txt
      - run: uvx pip-audit --strict -r req.txt
      - uses: google/osv-scanner-action@v1
        with:
          scan-args: |-
            -r
            --skip-git
            ./
```

The `schedule` is the part usually missing and the one that catches the most: most
CVEs are published **after** the merge. Without a periodic run, you only find out on
the next PR that happens to touch that file.

On PRs, scan only what changed so the feedback is actionable; on the scheduled run,
scan everything.

## What not to do

- **Upgrade everything at once in response to one alert.** Mixing the fix with twenty
  unrelated upgrades makes a regression impossible to isolate.
- **Silence without a record.** See `vulnerability-triage.md`.
- **Trust a single scanner.** Different databases cover different sets; the overlap
  is smaller than it looks.
- **Scan only on PRs.** Without a scheduled run, a CVE published after merge stays
  invisible for weeks.
