---
name: javascript
description: "Enforces default JavaScript coding standards for agents writing or refactoring JavaScript: module configuration, JSDoc contracts, runtime boundaries, testing, async, and performance. Use before editing JavaScript unless the repo provides stricter local rules."
version: "1.1.0"
category: development
triggers: javascript, js, jsx, mjs, cjs, jsconfig, package.json, eslint, biome, prettier, vitest, jest, node, browser
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for JavaScript projects without requiring a compiler."
  - "Secondary: SkillsMP JavaScript strictness, async, review, and error-handling guidance."
---

# JavaScript

Apply repository runtime, module, package, lint, framework, and generated-code rules first.

## Tooling

- Use the lockfile's package manager and configured formatter, lint or checked-JS,
  focused runtime tests, and relevant bundler checks.

## Configuration

- Preserve ESM/CJS intent, runtime targets, exports, JSX mode, lint globals, and
  browser or Node boundaries.
- Diagnostic hook: treat ESLint and checked-JS findings as contract evidence; avoid
  broad `any` JSDoc, casts, `@ts-ignore`, and disabled rules before fixing the value flow.

For package, module, lint, and check-JS setup:
`get_skill_file(name="javascript", path="references/configuration.md")`

## Contracts

- Use JSDoc, schemas, or runtime validators for shared and untrusted boundaries.
- Model variants with tagged objects and narrow by evidence before property access.

For JSDoc, schemas, and contract patterns:
`get_skill_file(name="javascript", path="references/types.md")`

## Error Handling

- Separate expected domain failures from exceptional failures and preserve `cause`
  when translating at process, request, job, or UI boundaries.
- Validate API, user, file, and environment data before use.

For error and boundary patterns:
`get_skill_file(name="javascript", path="references/error-handling.md")`

## Testing

- Use the repository's Vitest, Jest, Node test runner, or framework harness.
- Exercise runtime validation, module format, timers, and environment behavior that changed.

For test stack selection:
`get_skill_file(name="javascript", path="references/testing.md")`

## Concurrency

- Return or await promises, attach handling to deliberately detached work, and
  propagate `AbortSignal` through owned I/O.
- Bound fan-out and clean up timers, listeners, streams, workers, and subscriptions.

For cancellation, promises, and worker boundaries:
`get_skill_file(name="javascript", path="references/async.md")`

## Performance

- Use runtime or bundle evidence for parser, serializer, render-loop, allocation,
  and large-transform changes.

For runtime and bundle analysis:
`get_skill_file(name="javascript", path="references/performance.md")`

## API Design

- Define package surfaces with explicit exports and keep internals private.
- Prefer small functions and explicit dependencies over mutable module globals.
- Comment invariants, ownership, and surprising runtime constraints.
