# AGENTS.md

Instructions for AI coding agents working in this repository.

## What This Is

**Microsoft Agent Framework (MAF)** is an open, multi-language framework for
building production-grade AI agents and multi-agent workflows. It ships
parallel, API-consistent implementations in **Python** (`agent-framework` on
PyPI) and **.NET** (`Microsoft.Agents.AI` on NuGet), covering agent
abstractions, provider integrations (Azure OpenAI, OpenAI, Foundry,
Anthropic, Bedrock, Ollama, Gemini, Mistral, and more), graph-based
multi-agent workflow orchestration (sequential, concurrent, handoff, group
collaboration), middleware, observability (OpenTelemetry), declarative
(YAML) agent/workflow definitions, and hosting integrations.

This is a real repo split cleanly into two independently-built,
independently-tested language trees under one umbrella. **There is no
top-level build/test — always `cd python/` or `cd dotnet/` first.**

## Layout

| Path | Purpose |
|------|---------|
| `python/` | Python implementation — uv workspace, `packages/` (one pip package per provider/integration), `samples/`, `tests/`. Has its own [`python/AGENTS.md`](python/AGENTS.md). |
| `dotnet/` | .NET implementation — `src/` (one `Microsoft.Agents.AI.*` project per provider/integration), `tests/`, `samples/`. Has its own [`dotnet/AGENTS.md`](dotnet/AGENTS.md). |
| `declarative-agents/` | Standalone YAML agent (`agent-samples/`) and workflow (`workflow-samples/`) definitions, decoupled from either language's sample tree. |
| `docs/decisions/` | ADRs (architectural decision records) — numbered `NNNN-title.md`, templates at `adr-template.md` / `adr-short-template.md`. Propose architectural changes here. |
| `docs/design/`, `docs/features/`, `docs/specs/` | Design docs, feature write-ups, and specs. |
| `schemas/` | Shared JSON schemas (e.g. `durable-agent-entity-state.json`) consumed by both language implementations. |
| `.github/skills/` | Task-specific agent skill docs (build-and-test, project-structure, pull-requests, python-*) loaded on demand — see the per-language `AGENTS.md` for which ones apply. |
| `.github/copilot-instructions.md` | The GitHub Copilot equivalent of this file; keep the two in sync if repo-wide structure changes. |

## Commands

There is no root build. Pick a language tree:

```bash
# Python — from ./python
uv sync --dev && uv run poe install && uv run poe prek-install   # one-time setup (see python/DEV_SETUP.md)
uv run poe build                                                 # build
uv run poe test -A -m "not integration"                          # unit tests, all packages
uv run poe test -A -m integration                                # integration tests (needs API keys/endpoints)
uv run poe syntax                                                 # format + lint
uv run poe check                                                  # syntax + pyright + tests + sample checks (full gate)
uv run poe                                                         # list all available poe tasks

# .NET — from ./dotnet
dotnet build
dotnet test --filter-query "/*UnitTests*/*/*/*"
dotnet test --filter-query "/*IntegrationTests*/*/*/*"   # needs API keys/endpoints
dotnet format
```

Full detail: [`python/DEV_SETUP.md`](python/DEV_SETUP.md),
[`python/CODING_STANDARD.md`](python/CODING_STANDARD.md),
[`dotnet/README.md`](dotnet/README.md), and each tree's `AGENTS.md`.

## Conventions

- **Read the language-specific `AGENTS.md` before editing.** `python/AGENTS.md`
  and `dotnet/AGENTS.md` are the load-bearing instruction files for their
  trees — they cover coding standards, type annotations/XML docs, test
  structure, sample structure, and package-management conventions in detail.
  Many packages (Python) additionally have their own `packages/*/AGENTS.md`.
- **API and behavioral compatibility is required.** Both CONTRIBUTING.md and
  the .NET build enforce this: contributions that break public APIs are
  rejected outright, and .NET CI runs automated Package Validation
  (`dotnet build`/`dotnet pack` in Release config) against the last published
  NuGet baseline. An intentional, maintainer-approved breaking change needs a
  generated `CompatibilitySuppressions.xml` with justification — see
  CONTRIBUTING.md's "Breaking Changes" section.
- **Don't propose new public APIs without discussion first** — file an issue;
  don't surprise maintainers with large PRs. Small/trivial changes can skip
  the issue step.
- **Architectural decisions get an ADR.** Non-trivial design changes should
  add a numbered file under `docs/decisions/` using one of the two templates
  there, documenting alternatives considered and the rationale — see
  `docs/decisions/README.md`.
- **Terminology**: avoid "GA" for framework code/features — reserve it for
  hosted services (e.g. "the Foundry service is GA"). Use "released" or
  "stable" for Agent Framework packages/features (matches the Python
  feature-lifecycle stages).
- **Coding style**: Python follows Black-derived formatting enforced via
  `prek`/pre-commit hooks (`uv run poe syntax`); .NET follows standard
  Microsoft C# conventions with `dotnet format`. Prefer matching the existing
  style of the file/project you're editing when it diverges from the general
  guideline.
- **Samples are structured, not throwaway.** Both trees have dedicated sample
  guidelines (`python/samples/SAMPLE_GUIDELINES.md`,
  `dotnet/AGENTS.md`'s "Sample Structure" section): standalone project per
  sample, copyright/description header, env-var configuration (never
  hardcoded secrets), and a README per sample referenced from the parent
  README.

## Testing

- Each language tree keeps unit tests separate from integration tests, and
  integration tests require live API keys/endpoints (not run by default
  locally or in most CI jobs).
- Python: `uv run poe test -A -m "not integration"` for the full unit sweep,
  or scope to one package with `-P/--package`; `uv run poe check` is the
  complete local gate (syntax + pyright + tests + sample/markdown checks).
- .NET: `dotnet test --filter-query "/*UnitTests*/*/*/*"` per project or from
  the `dotnet/` root against the solution; test projects are the sibling
  `*.UnitTests`/`*.IntegrationTests` folders under `dotnet/tests/` named
  after the `src/` project they cover.
- CI (`.github/workflows/`) runs these per-language: `python-tests.yml`,
  `python-code-quality.yml`, `python-integration-tests.yml`,
  `python-sample-validation.yml` for Python; `dotnet-build-and-test.yml`,
  `dotnet-format.yml`, `dotnet-integration-tests.yml`,
  `dotnet-verify-samples.yml` for .NET; plus repo-wide `codeql-analysis.yml`
  and `markdown-link-check.yml`.

## Gotchas

- **This is a fork** (`RJHuey73/agent-framework`) of `microsoft/agent-framework`.
  PR guidance in `CONTRIBUTING.md` and the PR template refers to the upstream
  repo/branch conventions (branch off `main`, PR against `main`).
- **Command output capture (.NET)**: redirect `dotnet build`/`dotnet test`/
  `dotnet format` output to a file before analyzing it — these commands are
  expensive to re-run.
- **File encoding (.NET)**: new `.cs` files must be UTF-8 **with BOM**, or
  `dotnet format` will misbehave.
- **Python lazy loading**: provider integrations under `agent_framework/`
  (e.g. `agent_framework/azure/`) are lazy-loaded via `__getattr__`, so a
  package can be installed without pulling in every provider's dependencies.
- **Python packages vs. features have separate lifecycle stages** — see the
  `python-feature-lifecycle` skill referenced from `python/AGENTS.md` before
  assuming a package's version implies a feature's stability.
- Don't confuse `.github/copilot-instructions.md` (Copilot-facing, minimal)
  with this file — this one is meant to be more complete; update both when
  top-level structure changes.
