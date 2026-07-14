---
name: python
description: "Enforces default Python coding standards for agents writing or refactoring Python: project configuration, typing, error handling, testing, async, performance, and boundary validation. Use before editing Python unless the repo provides stricter local rules."
version: "1.1.0"
category: development
triggers: python, py, pyi, pyproject.toml, uv, ruff, mypy, pytest, tox, nox, typing, asyncio
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for Python projects and packaging/tooling conventions."
  - "Secondary: Python typing, asyncio, pytest, Ruff, and mypy guidance already used by Gobby's Python repo standards."
---

# Python

Apply repository packaging, interpreter, type-checker, framework, and generated-code rules first.

## Tooling

- Use the configured environment manager, formatter, Ruff, mypy or Pyright,
  focused pytest targets, and repository quality gates.

## Configuration

- Preserve Python constraints, build backend, dependency groups, lockfiles, package
  layout, type-checker mode, pytest config, and generated files.
- Diagnostic hook: treat type-checker and Ruff findings as value-flow evidence;
  avoid `Any`, `cast`, `# type: ignore`, and disabled rules before fixing the contract.

For package, tool, type-checker, and test setup:
`get_skill_file(name="python", path="references/configuration.md")`

## Type System

- Model domain states with dataclasses, enums, protocols, `TypedDict`, or validated
  models instead of unstructured dictionaries.
- Narrow untrusted input before domain construction and keep optionality explicit.

For typing patterns:
`get_skill_file(name="python", path="references/types.md")`

## Error Handling

- Catch specific exceptions and translate them at process, request, job, or CLI
  boundaries that own logging and cleanup.
- Preserve causes with `raise ... from ...` and keep branchable failures typed.

For exception hierarchies:
`get_skill_file(name="python", path="references/error-handling.md")`

## Testing

- Use repository fixtures, markers, async plugins, and boundary fakes.
- Parameterize genuine case tables and control clocks, filesystems, queues,
  databases, and HTTP adapters where behavior depends on them.

For pytest patterns:
`get_skill_file(name="python", path="references/testing.md")`

## Concurrency

- Use `asyncio.TaskGroup` for related tasks, explicit timeouts for owned I/O, and
  cleanup that re-raises `CancelledError`.
- Move CPU-bound or blocking clients behind an executor, process, or sync adapter.

For asyncio and concurrency:
`get_skill_file(name="python", path="references/async.md")`

## Performance

- Use profile evidence to choose data structures, streaming, caching, or allocation
  changes in parsers, serializers, database loops, and request paths.

For Python profiling:
`get_skill_file(name="python", path="references/performance.md")`

## API Design

- Prefer small typed return objects over `dict[str, object]`.
- Replace mode-changing boolean parameters with separate functions or enums.
- Use immutable defaults, explicit dependencies, and `pathlib.Path` at file boundaries.
- Put structured logs at owned boundaries with enough context to diagnose the operation.
