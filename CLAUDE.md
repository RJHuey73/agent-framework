# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

@AGENTS.md

`AGENTS.md`, imported above, is the canonical repo-wide agent guide — layout,
commands, compatibility rules, ADR process, testing, and gotchas. Keep changes
there; this file exists so Claude Code loads it (it auto-reads `CLAUDE.md`, not
`AGENTS.md`) and adds the navigation notes below.

## Orientation

**Microsoft Agent Framework (MAF)** ships parallel, API-consistent
implementations of the same framework in **Python** (`agent-framework` on PyPI)
and **.NET** (`Microsoft.Agents.AI` on NuGet): agent abstractions, provider
integrations, graph-based multi-agent workflow orchestration, middleware,
OpenTelemetry observability, declarative YAML definitions, and hosting.
This repo is `RJHuey73/agent-framework`, a fork of `microsoft/agent-framework`.

**There is no root build or root test.** Every command starts with `cd python/`
or `cd dotnet/`. The two trees build, test, and release independently.

## Instruction files are layered — read down to your tree

This repo has **32 `AGENTS.md` files**. They are not redundant; each is
load-bearing for its scope, and the nested ones are the ones with the actual
rules for the code you're editing:

1. `AGENTS.md` (root, imported above) — repo-wide structure and policy.
2. `python/AGENTS.md` or `dotnet/AGENTS.md` — **the load-bearing file for that
   tree**: coding standards, type annotations/XML docs, test layout, sample
   structure, package management. Read the one for your tree before editing.
3. `python/packages/<name>/AGENTS.md` (most Python packages have one) and
   `dotnet/samples/AGENTS.md` — package-local APIs and architecture.

Task-specific skills load on demand from `.github/skills/` at three levels:
`dotnet/.github/skills/` (`build-and-test`, `project-structure`,
`verify-dotnet-samples`, `verify-samples-tool`, `pull-requests`),
`python/.github/skills/` (`python-development`, `python-testing`,
`python-code-quality`, `python-feature-lifecycle`, `python-package-management`,
`python-samples`, `pull-requests`), and the repo root (`pull-requests`).

## Commands

```bash
# Python — from ./python
uv sync --dev && uv run poe install && uv run poe prek-install   # one-time (python/DEV_SETUP.md)
uv run poe test -A -m "not integration"     # unit tests, all packages
uv run poe test -P <package> -m "not integration"   # scope to one package
uv run poe syntax                            # format + lint
uv run poe check                             # the full local gate: syntax + pyright + tests + sample checks
uv run poe                                   # list every poe task

# .NET — from ./dotnet
dotnet build
dotnet test --filter-query "/*UnitTests*/*/*/*"
dotnet format
```

Integration tests (both trees) need live API keys/endpoints and don't run by
default.

## The rules most likely to bite

- **Public API compatibility is enforced, not aspirational.** Breaking a public
  API gets a PR rejected; .NET CI runs Package Validation against the last
  published NuGet baseline. An approved break needs a generated
  `CompatibilitySuppressions.xml` with justification.
- **Don't propose new public APIs without an issue first.** Large surprise PRs
  are explicitly unwelcome; trivial changes can skip it.
- **Non-trivial design changes need an ADR** under `docs/decisions/`, numbered,
  using one of the two templates there, with alternatives and rationale.
- **New `.cs` files must be UTF-8 with BOM**, or `dotnet format` misbehaves.
- **Redirect `dotnet build`/`test`/`format` output to a file** before analyzing
  — re-running them is expensive.
- **Python provider integrations are lazy-loaded** via `__getattr__`
  (`agent_framework/azure/`, etc.), so a package installs without pulling every
  provider's dependencies. Don't add an eager top-level import that defeats it.
- **Avoid "GA" for framework code** — that word is reserved for hosted services.
  Use "released" or "stable", matching the Python feature-lifecycle stages.
- **Packages and features have separate lifecycle stages** — a package version
  does not imply a feature's stability (`python-feature-lifecycle` skill).
- **Keep `.github/copilot-instructions.md` in sync** with the root `AGENTS.md`
  when repo-wide structure changes; it's the Copilot-facing equivalent.
