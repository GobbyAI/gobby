---
name: ruby
description: "Enforces default Ruby coding standards for agents writing or refactoring Ruby: Bundler/project configuration, object model contracts, Rails and data boundaries, errors, observability, tests, performance, concurrency, and deployment safety. Use before editing Ruby unless the repo provides stricter local rules."
version: "1.1.0"
category: development
triggers: ruby, rails, bundler, gemfile, rake, rspec, minitest, rubocop, sorbet, rbs
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for Ruby, Bundler, Rails, object model contracts, testing, and deployment workflows."
  - "Secondary: common Ruby and Rails project conventions around Bundler, RuboCop, RSpec, Minitest, Active Record, background jobs, observability, and MRI/JRuby runtime behavior."
---

# Ruby

Apply repository Ruby, Bundler, Rails, static-analysis, and deployment rules first.

## Tooling

- Use the checked-in Ruby/Bundler environment and configured RuboCop or Standard,
  Sorbet/Steep/RBS checks, focused tests, and relevant Rails tasks.

## Configuration

- Preserve Ruby and Bundler versions, gemspec and lockfile policy, autoloading,
  initializers, migrations, queues, runtime config, and generated files.
- Diagnostic hook: treat Sorbet, Steep, and RuboCop findings as contract evidence;
  avoid `T.untyped`, `T.unsafe`, broad signatures, and disabled cops before fixing flow.

For Bundler, Rails, type tools, and CI:
`get_skill_file(name="ruby", path="references/configuration.md")`

## Object Model And Contracts

- Preserve public methods, keyword arguments, callbacks, constants, inheritance,
  mixins, and serialized shapes.
- Use value objects, structs, enums, signatures, and pattern matching where they
  make domain states explicit.

For Ruby APIs and signatures:
`get_skill_file(name="ruby", path="references/object-model-and-contracts.md")`

## Data And Framework Boundaries

- Keep controllers, models, jobs, mailers, serializers, policies, and external
  services in their existing roles.
- Preserve transactions, authorization, callbacks, migrations, and job idempotency.

For Rails, Active Record, jobs, and services:
`get_skill_file(name="ruby", path="references/data-and-framework-boundaries.md")`

## Errors And Observability

- Follow local exception or result conventions and preserve original causes.
- Emit Logger, notifications, metrics, traces, and audit events at owned boundaries.

For failures, retries, transactions, and events:
`get_skill_file(name="ruby", path="references/errors-and-observability.md")`

## Testing

- Use the repository's RSpec, Minitest, Rails, fixture/factory, HTTP fake, and system
  test stack at the boundary being changed.
- Control database, job, clock, and external-service state explicitly.

For Ruby and Rails test selection:
`get_skill_file(name="ruby", path="references/testing.md")`

## Concurrency

- Make thread, fiber, Ractor, job, connection-pool, and shutdown ownership explicit
  for the deployed Ruby runtime.
- Account for MRI GVL versus JRuby or TruffleRuby behavior.

## Performance

- Inspect allocations, object retention, N+1 queries, eager loading, serialization,
  cache keys, and job throughput for affected workloads.

For runtime, query, cache, and concurrency analysis:
`get_skill_file(name="ruby", path="references/performance-and-concurrency.md")`
