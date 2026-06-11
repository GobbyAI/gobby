---
name: python
description: "Enforces default Python coding standards for agents writing or refactoring Python: project configuration, typing, error handling, testing, async, performance, and boundary validation. Use before editing Python unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: python, py, pyi, pyproject.toml, uv, ruff, mypy, pytest, tox, nox, typing, asyncio
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for Python projects and packaging/tooling conventions."
  - "Secondary: Python typing, asyncio, pytest, Ruff, and mypy guidance already used by Gobby's Python repo standards."
---

# Python

Default coding standards for Python. Repo conventions and configured tooling take precedence. If `pyproject.toml`, `setup.cfg`, test config, framework rules, or project instructions are stricter, follow the repo.

## Tooling

Run the repo's configured lint, type-check, and test commands before finishing. If none are configured, use:

- Format fix: `uv run ruff format <files>`
- Format verification: `uv run ruff format --check <files>`
- Lint verification: `uv run ruff check <files>`
- Type check: `uv run mypy <files-or-package>` or the repo's configured strict target
- Tests: targeted `GOBBY_TEST_PROTECT=1 uv run pytest <tests>` for changed behavior
- Packages: use the repo's Python package manager and lockfile, commonly `uv`

Do not suppress lint warnings with `# noqa`, mypy errors, broad ignores, or looser tool settings without a written reason tied to an external boundary or migration step.

## Configuration

- Treat `pyproject.toml` as the first place to inspect package metadata, Python version, build backend, dependencies, and tool settings.
- Keep formatter, linter, type checker, and test settings aligned. Do not fix a failure by weakening the config before understanding the code issue.
- Preserve `src/` layout, namespace packages, entry points, extras, and environment markers unless the task explicitly changes packaging.
- Keep generated files, lockfiles, vendored stubs, migrations, and pinned tool versions under their existing ownership rules.

For package, tool, type-checker, and test configuration: `get_skill_file(name="python", path="references/configuration.md")`

## Type System

- Type hints are mandatory on all function signatures: parameters and return values.
- Use modern syntax: `str | None`, `list[str]`, `dict[str, int]`.
- Keep untrusted or third-party values as `object` or `Any` only at the boundary, then narrow before passing inward.
- Prefer `Protocol`, dataclasses, typed dicts, enums, and small value objects over loose `dict[str, object]` plumbing.
- Use `TYPE_CHECKING`, local imports, or dependency inversion to avoid import cycles without hiding runtime dependencies.

For patterns and examples: `get_skill_file(name="python", path="references/types.md")`

## Error Handling

- Catch specific exceptions. Reserve `except Exception` for process, request, job, or CLI boundaries that own logging and cleanup.
- Chain with `raise NewError("context") from original` when wrapping lower-level failures.
- Validate external input, files, environment variables, database rows, and API responses before constructing typed domain objects.
- Keep domain errors machine-readable when callers need to branch; do not parse error messages.

For exception hierarchy patterns: `get_skill_file(name="python", path="references/error-handling.md")`

## Testing

- Use pytest and the repo's existing fixtures, markers, async plugin, and assertion style.
- Write one behavior per test with clear Arrange, Act, Assert phases.
- Mock external dependencies such as HTTP, databases, clocks, queues, and filesystems at boundary adapters.
- Parameterize related cases and test error paths, not only happy paths.
- Run targeted tests and the configured quality gates for the files you touched.

For fixtures, mocking, and marker patterns: `get_skill_file(name="python", path="references/testing.md")`

## Async

- Use `async def` for I/O-bound work. Keep CPU-bound work synchronous or move it to an executor/process pool when parallelism is needed.
- Prefer `asyncio.TaskGroup` for related tasks that share lifecycle and cancellation behavior.
- Set explicit timeouts on network, database, queue, subprocess, and long filesystem operations.
- Handle `asyncio.CancelledError` with cleanup and re-raise it.
- Do not call blocking clients inside `async def` without an adapter or executor.

For concurrency patterns: `get_skill_file(name="python", path="references/async.md")`

## Performance

- Profile before optimizing.
- Use data structures that match access patterns: `set` and `dict` for repeated lookup, generators and iterators for large streams, `deque` for queues.
- Cache only when inputs are stable and invalidation is clear.
- Avoid unnecessary object churn in parsers, serializers, database loops, and hot request paths.

For profiling tools and optimization rules: `get_skill_file(name="python", path="references/performance.md")`

## API & Design

- Prefer small typed return objects (`@dataclass`, Pydantic models) over `dict[str, object]`
- Avoid boolean flag parameters that create multiple behaviors; use separate functions or enums.
- Prefer immutable defaults and explicit dependency injection over hidden global state
- Use `pathlib.Path` for all file operations
- Set explicit timeouts on all network calls

## Observability

- Log at boundaries and failure points, not inside tight loops
- Never log secrets, credentials, or raw tokens
- Use structured logging with context (`logger.info("action", extra={...})`)

## Before You Finish

If you touched Python: verify formatting, lint, type checks, targeted tests, and any repo-specific validation pass before closing your work.
