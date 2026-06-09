---
name: php
description: "Enforces default PHP coding standards for agents writing or refactoring PHP: Composer configuration, typed API contracts, error handling, testing, framework boundaries, security, and performance. Use before editing PHP unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: php, composer, composer.json, composer.lock, phpunit, pest, phpstan, psalm, rector, laravel, symfony, doctrine
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for PHP runtimes, Composer, static analysis, and web frameworks."
  - "Secondary: PHP project conventions around strict types, Composer packaging, PSR standards, PHPUnit/Pest testing, framework boundaries, and web security."
---

# PHP

Default coding standards for PHP. Repo conventions and configured tooling take
precedence. If `composer.json`, `phpunit.xml`, PHPStan/Psalm config, Pint,
PHP-CS-Fixer, PHPCS, Rector, framework rules, or project instructions are
stricter, follow the repo.

## Tooling

Run the repo's configured format, lint/static analysis, type checks, and focused
tests before finishing. If none are configured, use:

- Format: the repo formatter, commonly Pint, PHP-CS-Fixer, PHPCS, or ECS
- Syntax/static checks: `php -l` for touched files plus configured PHPStan,
  Psalm, Rector dry run, or framework checks
- Tests: targeted PHPUnit or Pest tests for changed behavior
- Packages: Composer with the checked-in lockfile and scripts

Do not change PHP version constraints, Composer platform config, dependency
scopes, autoload rules, static-analysis levels, or framework config without a
written reason tied to the change.

## Configuration

- Match the repo's PHP version, extension requirements, Composer scripts,
  autoload rules, and lockfile policy.
- Keep `require`, `require-dev`, `autoload`, `autoload-dev`, and plugin config
  intentional.
- Preserve static-analysis baselines; do not hide new issues in a baseline.
- Use PSR and repo-local naming/layout conventions for namespaces, files, and
  service wiring.

For Composer, runtime, static-analysis, and style setup:
`get_skill_file(name="php", path="references/configuration.md")`

## Type And API Contracts

- Use `declare(strict_types=1);` when the repo already standardizes on it or for
  new strict packages.
- Model request data, responses, IDs, money, dates, and domain states with
  typed DTOs, value objects, enums, readonly classes, or framework-approved
  form/request objects.
- Avoid passing raw arrays, mixed values, dynamic properties, or unvalidated
  `stdClass` objects across boundaries.
- Use PHPDoc generics and array shapes where PHPStan/Psalm depends on them.

For strict types, PHPDoc, DTOs, enums, and collection contracts:
`get_skill_file(name="php", path="references/types.md")`

## Error Handling

- Translate framework, database, HTTP, filesystem, and extension failures at the
  boundary where the dependency is known.
- Preserve previous exceptions when adding domain context.
- Treat PHP warnings, false-return APIs, resource handles, and partial writes as
  explicit error paths.
- Keep user-facing messages safe and logs useful without exposing secrets.

For exceptions, false-return APIs, resources, and boundary translation:
`get_skill_file(name="php", path="references/error-handling.md")`

## Testing

- Add focused PHPUnit or Pest tests for changed behavior, validation failures,
  authorization checks, serialization, and framework wiring that matters.
- Prefer narrow unit tests for domain code and slice/integration tests for HTTP,
  queue, ORM, container, or event behavior.
- Run static analysis on the touched namespaces when type contracts change.

For PHPUnit, Pest, framework tests, fixtures, and command selection:
`get_skill_file(name="php", path="references/testing.md")`

## Framework Boundaries

- Keep controllers, commands, jobs, listeners, middleware, repositories, and ORM
  models at the edge when core behavior can remain plain PHP.
- Validate framework-bound input before it reaches domain services.
- Use constructor injection and explicit dependencies rather than service
  locators, facades, or globals unless the repo's framework style requires them.

For Laravel, Symfony, Doctrine/Eloquent, PSR, DI, and serialization boundaries:
`get_skill_file(name="php", path="references/framework-boundaries.md")`

## Security

- Treat request data, headers, cookies, files, sessions, env vars, database rows,
  queues, and webhooks as untrusted input.
- Validate authorization, CSRF, SSRF, file upload, path traversal, SQL/query,
  command execution, template escaping, and serialization boundaries where the
  change touches them.
- Keep secrets out of logs, exceptions, fixtures, and client responses.

For PHP web and framework security checks:
`get_skill_file(name="php", path="references/security.md")`

## Performance

- Profile before optimizing and use application metrics, Blackfire, Xdebug,
  Tideways, query plans, or focused benchmarks where appropriate.
- Avoid hidden N+1 queries, excessive hydration, unbounded arrays, repeated
  reflection/container work, and memory-heavy transforms.
- Use generators, streaming responses, pagination, caching, and batch work only
  with clear ownership and invalidation.

For memory, I/O, database, cache, and runtime performance:
`get_skill_file(name="php", path="references/performance.md")`

## Before You Finish

If you touched PHP: verify formatting/static analysis where configured,
targeted PHPUnit/Pest tests, and focused framework or Composer checks relevant
to the changed code.
