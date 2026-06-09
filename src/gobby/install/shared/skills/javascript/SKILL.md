---
name: javascript
description: "Enforces default JavaScript coding standards for agents writing or refactoring JavaScript: module configuration, JSDoc contracts, runtime boundaries, testing, async, and performance. Use before editing JavaScript unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: javascript, js, jsx, mjs, cjs, jsconfig, package.json, eslint, biome, prettier, vitest, jest, node, browser
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for JavaScript projects without requiring a compiler."
  - "Secondary: SkillsMP JavaScript strictness, async, review, and error-handling guidance."
---

# JavaScript

Default coding standards for JavaScript. Repo conventions and configured tooling take precedence. If `package.json`, `jsconfig.json`, `eslint.config.*`, framework rules, or project instructions are stricter, follow the repo.

## Tooling

Run the repo's configured format, lint, type-aware check, and test commands before finishing. If none are configured, use:

- Format: the repo formatter, commonly `prettier --check .` or `biome check .`
- Lint: `eslint .` with strict rules for unused code, promises, imports, and unsafe globals
- Type check: `tsc -p jsconfig.json --noEmit --allowJs --checkJs` when configured
- Tests: targeted `vitest`, `jest`, `node --test`, or framework-local tests
- Packages: use the repo's package manager lockfile (`pnpm`, `npm`, `yarn`, or `bun`)

Do not add broad lint disables, unchecked dynamic property chains, silent catches, or global mutations without a written reason tied to an external boundary or migration step.

## Configuration

- Make the module system explicit with `package.json` `type`, file extensions, and package `exports`.
- Prefer ESM for new app code; use CommonJS only when the runtime or dependency surface requires it.
- Enable strict linting for promises, imports, equality, unused values, and accidental globals.
- Use `jsconfig.json` or checked JSDoc when a JavaScript package needs editor and CI contract checks.

For package, module, lint, and check-JS setup: `get_skill_file(name="javascript", path="references/configuration.md")`

## Contracts

- Model important object shapes with JSDoc typedefs or small schema validators.
- Validate untrusted API responses, user input, files, environment variables, and message payloads before property access.
- Prefer explicit object construction and named fields over loose dictionaries passed through layers.
- Keep TypeScript migration easy: avoid patterns that hide contracts behind mutation, monkey-patching, or implicit globals.

For JSDoc, schemas, and contract patterns: `get_skill_file(name="javascript", path="references/types.md")`

## Error Handling

- Throw `Error` instances, not strings or plain objects.
- Treat expected domain failures as explicit result objects or error variants.
- Check `fetch` status, parse failures, and missing fields before returning data.
- Add context with `cause` at boundaries, and preserve original stack traces.

For error and boundary patterns: `get_skill_file(name="javascript", path="references/error-handling.md")`

## Testing

- Add targeted runtime tests for behavior, boundary validation, and error paths.
- Prefer real module imports and public APIs; mock network, timers, storage, and process boundaries.
- Use fake timers only when the test controls all scheduled work.
- Cover both ESM and CommonJS entry points when the package exports both.

For test stack selection and examples: `get_skill_file(name="javascript", path="references/testing.md")`

## Async

- Always return or await promises; do not leave floating promises unless deliberately detached with error handling.
- Use `AbortController`, explicit timeouts, and cleanup for I/O that can outlive a request or component.
- Limit concurrency intentionally instead of running unbounded `Promise.all` over external inputs.
- Preserve error context across async boundaries.

For cancellation, concurrency, and promise patterns: `get_skill_file(name="javascript", path="references/async.md")`

## Performance

- Profile before optimizing hot paths.
- Avoid unnecessary object churn in render loops, parsers, serializers, and large transforms.
- Use sets, maps, stable keys, memoization, streaming, and incremental parsing where the workload justifies them.
- Keep dependency and bundle costs visible when adding runtime packages.

For runtime and bundle performance patterns: `get_skill_file(name="javascript", path="references/performance.md")`

## API & Design

- Define public package surfaces with explicit exports.
- Keep internal modules private by convention or package `exports`.
- Prefer plain functions, injected dependencies, and small modules over framework-independent global state.
- Use comments only for non-obvious invariants, runtime compatibility, or suppression justifications.

## Before You Finish

If you touched JavaScript: verify formatting/lint, targeted runtime tests, and any configured checked-JS or bundler checks pass before closing your work.
