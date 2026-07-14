---
name: php
description: "Enforces default PHP coding standards for agents writing or refactoring PHP: Composer configuration, typed API contracts, error handling, testing, framework boundaries, security, and performance. Use before editing PHP unless the repo provides stricter local rules."
version: "1.1.0"
category: development
triggers: php, composer, composer.json, composer.lock, phpunit, pest, phpstan, psalm, rector, laravel, symfony, doctrine
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for PHP runtimes, Composer, static analysis, and web frameworks."
  - "Secondary: PHP project conventions around strict types, Composer packaging, PSR standards, PHPUnit/Pest testing, framework boundaries, and web security."
---

# PHP

Apply repository runtime, Composer, static-analysis, framework, and generated-code rules first.

## Tooling

- Use the lockfile's PHP/Composer environment and configured format, PHPStan or
  Psalm, Rector, focused PHPUnit or Pest, and framework commands.

## Configuration

- Preserve PHP constraints, extensions, autoloading, package type, scripts, lockfile,
  analysis level, framework cache, and generated files.
- Diagnostic hook: treat PHPStan and Psalm findings as boundary evidence; avoid
  baselines, ignores, `mixed`, and broad casts before modeling the actual type.

For Composer, runtime, analysis, and generated-code setup:
`get_skill_file(name="php", path="references/configuration.md")`

## Type And API Contracts

- Use strict types, typed properties, enums, value objects, readonly state, and
  explicit DTOs for domain contracts.
- Validate request, CLI, environment, database, and deserialized values at entry.

For PHP types and package APIs:
`get_skill_file(name="php", path="references/types.md")`

## Error Handling

- Follow local exception or result conventions, preserve previous causes, and map
  failures at HTTP, queue, CLI, transaction, or job boundaries.
- Bind streams, locks, transactions, and temporary resources to cleanup.

For exception and cleanup patterns:
`get_skill_file(name="php", path="references/error-handling.md")`

## Testing

- Use repository PHPUnit/Pest data providers, fixtures, clocks, fakes, and framework
  harnesses at the boundary being changed.
- Keep database, serialization, HTTP, and queue coverage real when those are contracts.

For test selection:
`get_skill_file(name="php", path="references/testing.md")`

## Framework Boundaries

- Keep Laravel/Symfony controllers, middleware, containers, ORM, queues, and
  serializers as adapters around explicit domain behavior.
- Preserve migrations, transactions, authorization, validation, and job idempotency.

For framework and persistence boundaries:
`get_skill_file(name="php", path="references/framework-boundaries.md")`

## Security

- Use framework escaping, parameterized queries, CSRF/auth controls, safe upload
  handling, and the repository secret mechanism at the relevant trust boundary.

For web and supply-chain risks:
`get_skill_file(name="php", path="references/security.md")`

## Performance

- Inspect query count, hydration, serialization, autoloading, memory, cache keys,
  and worker lifetime before applying caching, batching, or lower-level APIs.

For PHP and framework analysis:
`get_skill_file(name="php", path="references/performance.md")`
