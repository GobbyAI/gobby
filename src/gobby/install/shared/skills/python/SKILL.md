---
name: python
description: "Enforces default Python coding standards for agents writing or refactoring Python: project configuration, typing, error handling, testing, async, performance, and boundary validation. Use before editing Python unless the repo provides stricter local rules."
version: "1.2.1"
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
- Diagnostic hook: treat type-checker and Ruff findings as value-flow evidence and
  fix the underlying contract before considering a suppression.

For package, tool, type-checker, and test setup:
`get_skill_file(name="python", path="references/configuration.md")`

## Lint and Type Suppressions

`# noqa` and `# type: ignore` disable defect detection. Suppressions are a last resort,
allowed only when all of these hold:

1. A root-cause fix was attempted first: accurate types, control flow, a narrow
   adapter, a `Protocol`, or a local stub.
2. The remaining diagnostic comes from exactly one of:
   incorrect or incomplete third-party type information outside repository control;
   a confirmed linter or type-checker defect or unsupported language feature;
   runtime-required dynamic behavior or import side effect the analyzer cannot model.
3. It is scoped to one expression or statement and names the exact diagnostic
   (`# noqa: <rule-code>` or `# type: ignore[<error-code>]`), with an adjacent comment
   naming the external limitation, why runtime behavior is safe, and any upstream issue.
4. A regression test covers the runtime behavior; focused lint, type-check, and tests rerun.

Bare suppressions are prohibited, as are file-wide ignores, configuration exclusions,
and relaxed checker settings used as substitutes for fixing diagnostics. Repository
policy may forbid suppressions even under the conditions above.

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
- Parameterize genuine case tables; control clocks, filesystems, queues, databases,
  and HTTP adapters where behavior depends on them.

For pytest patterns:
`get_skill_file(name="python", path="references/testing.md")`

## Concurrency

- Use `asyncio.TaskGroup`, explicit timeouts for owned I/O, and cleanup that
  re-raises `CancelledError`.
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
- Use immutable defaults, explicit dependencies, `pathlib.Path` at file boundaries,
  and structured logs at owned boundaries with enough context to diagnose the operation.
