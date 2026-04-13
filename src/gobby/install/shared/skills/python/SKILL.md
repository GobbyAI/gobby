---
name: python
description: "Enforces default Python coding standards for agents writing or refactoring Python: typing, error handling, testing, async, and boundary validation. Use before editing Python unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: python, type hints, pytest, ruff, mypy, async
---

# Python

Default coding standards for Python. **Repo conventions and configured tooling take precedence** — if `pyproject.toml`, `CLAUDE.md`, or project rules specify stricter or different standards, follow those instead.

---

## Tooling

Run the repo's configured lint, type-check, and test commands before finishing. If none are configured, use:

- **Format + lint**: `ruff format . && ruff check . --fix`
- **Type check**: `mypy --strict`
- **Tests**: `pytest` targeting changed code, not the full suite
- **Packages**: `uv` for dependency management

Don't suppress lint warnings with `# noqa` without a documented reason.

## Type System

- Type hints are mandatory on all function signatures (parameters + return)
- Modern syntax: `str | None`, `list[str]`, `dict[str, int]` — not `Optional`, `List`, `Dict`
- Do not introduce `Any` in application code unless forced by an external boundary; narrow it immediately with a wrapper, cast, `Protocol`, or stub
- Use `TYPE_CHECKING` to break circular imports
- Prefer `Protocol` over ABC for structural typing

For patterns and examples: `get_skill_file(name="python", path="references/types.md")`

## Error Handling

- Catch specific exceptions — never bare `except:`
- `except Exception:` is acceptable only at process boundaries (CLI entrypoints, server handlers, task runners) for logging and cleanup
- Chain with `raise NewError("context") from original`
- Guard clauses: validate early, return early, happy path last
- Validate at boundaries (API layer, CLI args, external input) — keep core logic pure
- Wrap third-party exceptions in domain exceptions at boundaries

For exception hierarchy patterns: `get_skill_file(name="python", path="references/error-handling.md")`

## Testing

- **pytest only.** AAA pattern (Arrange -> Act -> Assert), one behavior per test
- Mock external dependencies (HTTP, databases, filesystems), not internals — with narrow exceptions for hard seams in legacy code
- Parameterize for input variations instead of duplicating tests
- Run targeted tests for changed code; enforce the repo's coverage threshold

For fixtures, mocking, and marker patterns: `get_skill_file(name="python", path="references/testing.md")`

## Async

- I/O-bound -> `async def`. CPU-bound -> sync + `multiprocessing` if parallelism needed
- Prefer `asyncio.TaskGroup` over ad hoc `create_task()` when task lifecycle matters
- Set explicit timeouts on all async operations
- Handle `CancelledError` and implement cleanup in long-running tasks
- Never call blocking/sync functions inside `async def` without `run_in_executor`

For concurrency patterns: `get_skill_file(name="python", path="references/async.md")`

## Performance

- **Profile before optimizing** — gut feelings about bottlenecks are usually wrong
- Use appropriate data structures: `set`/`dict` for O(1) lookups, generators for large data
- `functools.cache` / `lru_cache` for repeated expensive computation
- Comprehensions over loops for simple transforms

For profiling tools and optimization rules: `get_skill_file(name="python", path="references/performance.md")`

## API & Design

- Prefer small typed return objects (`@dataclass`, Pydantic models) over `dict[str, object]`
- Avoid boolean flag parameters that create multiple behaviors — use separate functions or enums
- Prefer immutable defaults and explicit dependency injection over hidden global state
- Use `pathlib.Path` for all file operations
- Set explicit timeouts on all network calls

## Observability

- Log at boundaries and failure points, not inside tight loops
- Never log secrets, credentials, or raw tokens
- Use structured logging with context (`logger.info("action", extra={...})`)

## Before You Finish

If you touched Python: verify formatting, lint, types, and targeted tests pass before closing your work.
