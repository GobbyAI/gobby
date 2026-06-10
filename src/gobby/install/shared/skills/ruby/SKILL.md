---
name: ruby
description: "Enforces default Ruby coding standards for agents writing or refactoring Ruby: Bundler/project configuration, object model contracts, Rails and data boundaries, errors, observability, tests, performance, concurrency, and deployment safety. Use before editing Ruby unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: ruby, rails, bundler, gemfile, rake, rspec, minitest, rubocop, sorbet, rbs
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for Ruby, Bundler, Rails, object model contracts, testing, and deployment workflows."
  - "Secondary: common Ruby and Rails project conventions around Bundler, RuboCop, RSpec, Minitest, Active Record, background jobs, observability, and MRI/JRuby runtime behavior."
---

# Ruby

Default coding standards for Ruby. Repo conventions and configured tooling take
precedence. If `.ruby-version`, `Gemfile`, RuboCop, Standard, RSpec, Minitest,
Rails, Sorbet, Steep, RBS, or project instructions are stricter, follow the repo.

## Tooling

Run the repo's configured format, lint, type/static analysis, and focused test
commands before finishing. If none are configured, use the local Ruby project:

- Format/lint: `bundle exec rubocop` or configured Standard/RuboCop wrapper
- Type/static analysis: Sorbet, Steep, RBS, ruby-lsp, or project wrapper
- Tests: focused RSpec, Minitest, Rails test, or engine/gem target
- Packages: preserve Bundler, lockfile, gemspec, and Ruby version policy
- Runtime checks: background job, migration, system test, or smoke target where used

Do not loosen lint rules, remove type signatures, bypass Bundler, delete lockfile
constraints, or broaden tests to avoid fixing a local defect.

## Configuration

- Match existing Ruby version, Bundler, gemspec, Rails, engine, RuboCop,
  Standard, Sorbet, Steep, RBS, CI, and deployment conventions before adding
  files or gems.
- Keep dependency, autoloading, initializer, environment, migration, queue, and
  packaging changes intentional.
- Prefer standard library, framework APIs, and existing project helpers before
  adding gems.

For Bundler, gemspecs, Ruby versions, Rails config, lint/type tools, and CI:
`get_skill_file(name="ruby", path="references/configuration.md")`

## Object Model And Contracts

- Treat public classes, modules, mixins, service objects, jobs, serializers,
  policies, callbacks, and gem APIs as contracts.
- Use keyword arguments, value objects, structs, enums, pattern matching,
  explicit return objects, RBS, Sorbet, or Steep where the repo uses them.
- Preserve method arity, keyword compatibility, callback behavior, constants,
  inheritance hooks, and API return shapes unless the change is intentional.

For classes, modules, APIs, keyword args, signatures, value objects, and mixins:
`get_skill_file(name="ruby", path="references/object-model-and-contracts.md")`

## Data And Framework Boundaries

- Keep Rails controllers, views, models, jobs, mailers, serializers, policies,
  queries, external clients, and domain objects separated by clear boundaries.
- Validate and normalize input at edges before it reaches domain code.
- Preserve transactions, idempotency, authorization, callbacks, migrations, and
  data compatibility when moving behavior.

For Rails, Active Record, background jobs, serializers, external services, and
boundary design:
`get_skill_file(name="ruby", path="references/data-and-framework-boundaries.md")`

## Errors And Observability

- Follow local conventions for exceptions, result objects, dry-monads, Active
  Model errors, transaction rollback, retries, and user-facing errors.
- Preserve original error context and avoid converting structured failures into
  vague strings, symbols, or swallowed exceptions.
- Emit Logger, ActiveSupport::Notifications, metrics, traces, and audit events
  where surrounding code already does so.

For exceptions, result shapes, retries, transactions, logging, notifications,
and audit events:
`get_skill_file(name="ruby", path="references/errors-and-observability.md")`

## Testing

- Add focused coverage for changed classes, service boundaries, validations,
  query behavior, background jobs, mailers, controllers, views, and failures.
- Use the repo's stack: RSpec, Minitest, Rails test, FactoryBot, fixtures,
  WebMock, VCR, Capybara, Shoulda, Timecop, or custom helpers.
- Prefer deterministic unit and boundary tests before broad suite runs.

For RSpec, Minitest, Rails tests, fixtures, factories, HTTP fakes, system tests,
and focused commands:
`get_skill_file(name="ruby", path="references/testing.md")`

## Performance And Concurrency

- Measure hot paths before optimizing. Check allocations, N+1 queries, eager
  loading, memoization, caches, fibers, threads, jobs, and connection pools.
- Keep concurrency explicit around MRI GVL behavior, JRuby/TruffleRuby,
  Ractors, Fiber Scheduler, async gems, queues, and database connections.
- Use batching, streaming, prepared queries, indexes, caching, or background jobs
  only with evidence and tests.

For Ruby runtime performance, Rails query shape, memory, caching, jobs, threads,
fibers, and deployment:
`get_skill_file(name="ruby", path="references/performance-and-concurrency.md")`

## Before You Finish

If you touched Ruby: verify formatting/lint, configured type/static analysis,
focused tests, and any relevant migration/job/runtime checks pass before closing
your work.
