---
name: elixir
description: "Enforces default Elixir coding standards for agents writing or refactoring Elixir: Mix configuration, OTP supervision, processes, typespecs, Phoenix/Ecto boundaries, errors, observability, tests, releases, and BEAM performance. Use before editing Elixir unless the repo provides stricter local rules."
version: "1.1.0"
category: development
triggers: elixir, mix, exunit, phoenix, ecto, otp, genserver, supervision, dialyzer, credo
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for Elixir Mix projects, OTP supervision, process ownership, Phoenix/Ecto boundaries, typespecs, observability, ExUnit, releases, and BEAM runtime behavior."
  - "Secondary: common Elixir/OTP project conventions around Mix tooling, formatter and Credo settings, Dialyzer, supervision trees, data boundaries, async tests, telemetry, and release configuration."
---

# Elixir

Apply repository Mix, OTP, Phoenix/Ecto, release, and static-analysis rules first.

## Tooling

- Use configured `mix format`, Credo, Dialyzer, Sobelow, warnings-as-errors
  compilation, and focused ExUnit targets.
- Scope umbrella commands to the affected application where possible.

## Configuration

- Preserve dependency, application, supervision, endpoint, router, migration,
  runtime-config, and release boundaries.
- Diagnostic hook: treat Dialyzer and compiler mismatches as return-shape evidence;
  avoid widening specs to `term()` or adding ignores before checking the real flow.

For Mix, umbrella, analysis, release, and CI setup:
`get_skill_file(name="elixir", path="references/configuration.md")`

## Types And Contracts

- Keep public modules, behaviours, callbacks, structs, schemas, contexts, protocols,
  and message formats compatible.
- Use pattern matching, guards, typespecs, and opaque types to expose valid states.

For module contracts and return shapes:
`get_skill_file(name="elixir", path="references/types-and-contracts.md")`

## Concurrency

- Define process ownership, supervision, restart behavior, message protocol,
  timeout, back-pressure, and shutdown behavior.
- Prefer supervised OTP primitives and keep process state observable and recoverable.

For supervision, tasks, registries, and process state:
`get_skill_file(name="elixir", path="references/otp-and-concurrency.md")`

## Data And Boundaries

- Keep Phoenix controllers, LiveViews, contexts, Ecto schemas, changesets, and
  external services in their existing layers.
- Preserve transaction, idempotency, migration, and authorization boundaries.

For Phoenix, Ecto, and service design:
`get_skill_file(name="elixir", path="references/data-and-boundaries.md")`

## Errors And Observability

- Follow local result-tuple and exception conventions while preserving causes.
- Emit Logger, Telemetry, metrics, traces, and audit events at owned boundaries.

For errors, retries, and events:
`get_skill_file(name="elixir", path="references/errors-and-observability.md")`

## Testing

- Exercise changed behaviours, process messages, supervision, changesets,
  transactions, and Phoenix boundaries with the repository's ExUnit stack.
- Respect async-safety and SQL-sandbox ownership.

For ExUnit, mocks, property checks, and commands:
`get_skill_file(name="elixir", path="references/testing.md")`

## Performance And Releases

- Inspect reductions, mailbox growth, binary retention, scheduler pressure,
  query shape, and supervision topology for affected workloads.
- Keep release config, secrets, clustering, migrations, and rollback behavior aligned.

For BEAM analysis and deployment:
`get_skill_file(name="elixir", path="references/performance-and-releases.md")`
