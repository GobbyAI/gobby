---
name: typescript
description: "Enforces default TypeScript coding standards for agents writing or refactoring TypeScript: strict compiler configuration, type modeling, runtime boundaries, testing, async, and performance. Use before editing TypeScript unless the repo provides stricter local rules."
version: "1.1.0"
category: development
triggers: typescript, tsconfig, tsx, mts, cts, vitest, tsd, eslint, typescript-eslint
sources:
  - "Primary: CUBETIQ cubis-foundry typescript-best-practices skill, MIT-licensed in package.json, adapted for Gobby's bundled language-skill layout."
  - "Secondary SkillsMP checks: javascript-strict, review-typescript, async-expert, handling-errors, rime-js."
---

# TypeScript

Apply repository compiler, runtime, module, package, lint, and framework rules first.

## Tooling

- Use the lockfile's package manager and configured formatter, type-aware lint,
  `tsc --noEmit`, focused runtime tests, and public type tests.

## Configuration

- Preserve strictness, runtime/module targets, resolution, project references,
  declaration output, path aliases, JSX mode, and generated files.
- Diagnostic hook: follow the compiler diagnostic to the mismodeled boundary; avoid
  `as any`, `@ts-ignore`, broad assertions, and weakened strictness as escape hatches.

For compiler options and migrations:
`get_skill_file(name="typescript", path="references/configuration.md")`

## Type System

- Model variants with discriminated unions and exhaustive checks.
- Use `unknown` at untrusted boundaries and narrow with schemas, guards, or assertions.
- Reserve advanced conditional or mapped types for shared contracts that earn them.

For type patterns:
`get_skill_file(name="typescript", path="references/types.md")`

## Error Handling

- Represent expected failures explicitly and translate exceptional failures at
  process, request, job, or UI boundaries.
- Catch `unknown`, narrow it, preserve `cause`, and validate external data before use.

For error and boundary patterns:
`get_skill_file(name="typescript", path="references/error-handling.md")`

## Testing

- Pair runtime behavior tests with type tests for public contracts.
- Use `@ts-expect-error` for negative cases so stale suppressions fail.

For test stack selection:
`get_skill_file(name="typescript", path="references/testing.md")`

## Concurrency

- Return or await promises, handle deliberately detached work, and propagate
  `AbortSignal` and timeouts through owned I/O.
- Bound fan-out and clean up listeners, streams, workers, and subscriptions.

For cancellation and promise patterns:
`get_skill_file(name="typescript", path="references/async.md")`

## Performance

- Use runtime and compiler evidence for recursive types, object churn, render loops,
  parsers, serializers, and large transforms.

For runtime and compiler analysis:
`get_skill_file(name="typescript", path="references/performance.md")`

## API Design

- Define package surfaces with explicit value and type exports.
- Keep internals private and dependencies explicit.
- Comment non-obvious type design, invariants, and justified suppressions.
