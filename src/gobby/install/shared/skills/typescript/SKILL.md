---
name: typescript
description: "Enforces default TypeScript coding standards for agents writing or refactoring TypeScript: strict compiler configuration, type modeling, runtime boundaries, testing, async, and performance. Use before editing TypeScript unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: typescript, tsconfig, tsx, mts, cts, vitest, tsd, eslint, typescript-eslint
sources:
  - "Primary: CUBETIQ cubis-foundry typescript-best-practices skill, MIT-licensed in package.json, adapted for Gobby's bundled language-skill layout."
  - "Secondary SkillsMP checks: javascript-strict, review-typescript, async-expert, handling-errors, rime-js."
---

# TypeScript

Default coding standards for TypeScript. Repo conventions and configured tooling take precedence. If `tsconfig.json`, `eslint.config.*`, `package.json`, framework rules, or project instructions are stricter, follow the repo.

## Tooling

Run the repo's configured format, lint, type-check, and test commands before finishing. If none are configured, use:

- Format: the repo formatter, commonly `prettier --check .` or `biome check .`
- Lint: `eslint .` with type-aware `typescript-eslint` rules when configured
- Type check: `tsc -p tsconfig.json --noEmit`
- Tests: targeted `vitest`, `jest`, `tsx --test`, or framework-local tests
- Packages: use the repo's package manager lockfile (`pnpm`, `npm`, `yarn`, or `bun`)

Do not add `any`, `as unknown as`, `// @ts-ignore`, disabled ESLint rules, or loose compiler flags without a written reason tied to an external boundary or migration step.

## Configuration

- Keep `strict: true` as the baseline.
- Enable `noUncheckedIndexedAccess` and `exactOptionalPropertyTypes` for new or already-strict packages.
- Use `moduleResolution: "bundler"` for bundled apps and `module/moduleResolution: "NodeNext"` for Node libraries or CLIs.
- Prefer `verbatimModuleSyntax: true` and `import type` for type-only imports.
- Avoid legacy runtime-emitting TypeScript syntax such as enums, namespaces, parameter properties, and `const enum` in new code.

For compiler options and migration sequencing: `get_skill_file(name="typescript", path="references/configuration.md")`

## Type System

- Model variant states with discriminated unions and exhaustive `never` checks.
- Prefer `unknown` at untrusted boundaries; narrow with schemas, type guards, or assertion functions.
- Use `satisfies` to validate object shapes while preserving literals.
- Use branded types for domain primitives such as IDs, emails, tokens, and URLs.
- Reserve conditional, mapped, and template-literal types for shared APIs or utilities where the extra complexity buys real safety.

For patterns and examples: `get_skill_file(name="typescript", path="references/types.md")`

## Error Handling

- Treat expected domain failures as typed results or explicit error variants.
- Throw or reject for exceptional failures, then catch at process, request, job, or UI boundary layers.
- Catch `unknown`, narrow it, and rethrow with `cause` when adding context.
- Validate API responses, user input, file data, and environment variables before branding or trusting them.

For error and boundary patterns: `get_skill_file(name="typescript", path="references/error-handling.md")`

## Testing

- Add runtime tests for behavior and type tests for public type contracts.
- Use `expectTypeOf` in Vitest when present; use `tsd` or compile-only assertion files for packages exposing types.
- Use `// @ts-expect-error` for negative type tests because it fails when the error disappears.
- Test boundary validation and error paths, not only happy paths.

For test stack selection and examples: `get_skill_file(name="typescript", path="references/testing.md")`

## Async

- Always return or await promises; never leave floating promises unless deliberately detached with error handling.
- Use `AbortSignal`, explicit timeouts, and cleanup for I/O that can outlive a request or component.
- Limit concurrency intentionally instead of spraying `Promise.all` across unbounded inputs.
- Preserve error context across async boundaries.

For cancellation, concurrency, and promise patterns: `get_skill_file(name="typescript", path="references/async.md")`

## Performance

- Profile before optimizing hot paths; do not trade readable types for speculative speed.
- Keep heavy recursive utility types out of frequently edited application surfaces.
- Avoid unnecessary object churn in render loops, parsers, serializers, and large transforms.
- Use sets, maps, stable keys, memoization, and streaming/iterative transforms where the workload justifies them.

For runtime and compiler performance patterns: `get_skill_file(name="typescript", path="references/performance.md")`

## API & Design

- Define public package surfaces with explicit exports and type-only exports.
- Keep internal modules private by convention or package `exports`.
- Prefer plain functions, typed dependencies, and small modules over framework-independent global state.
- Use comments only for non-obvious type design, invariants, or suppression justifications.

## Before You Finish

If you touched TypeScript: verify formatting/lint, `tsc`, targeted runtime tests, and any relevant type tests pass before closing your work.
