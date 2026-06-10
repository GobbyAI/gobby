---
name: elixir
description: "Enforces default Elixir coding standards for agents writing or refactoring Elixir: Mix configuration, OTP supervision, processes, typespecs, Phoenix/Ecto boundaries, errors, observability, tests, releases, and BEAM performance. Use before editing Elixir unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: elixir, mix, exunit, phoenix, ecto, otp, genserver, supervision, dialyzer, credo
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for Elixir Mix projects, OTP supervision, process ownership, Phoenix/Ecto boundaries, typespecs, observability, ExUnit, releases, and BEAM runtime behavior."
  - "Secondary: common Elixir/OTP project conventions around Mix tooling, formatter and Credo settings, Dialyzer, supervision trees, data boundaries, async tests, telemetry, and release configuration."
---

# Elixir

Default coding standards for Elixir. Repo conventions and configured tooling take
precedence. If formatter settings, Credo/Dialyzer rules, Phoenix/Ecto context
patterns, OTP architecture docs, release config, or project instructions are
stricter, follow the repo.

## Tooling

Run the repo's configured format, static analysis, compile, and focused test
commands before finishing. If none are configured, use the local Mix project:

- Format: `mix format` scoped by repo wrapper or touched files when available
- Analyze: configured Credo, Dialyzer, Sobelow, or CI wrapper
- Compile: `mix compile --warnings-as-errors` or project equivalent
- Tests: focused ExUnit file, line, tag, umbrella app, or integration target
- Runtime checks: telemetry, supervision, release, or property/fuzz target where used

Do not silence warnings, hide supervision failures, remove typespecs, disable
analysis, or broaden tests to avoid fixing a local defect.

## Configuration

- Match existing Mix, umbrella, formatter, Credo, Dialyzer, Phoenix, Ecto,
  release, runtime config, and environment conventions before adding files.
- Keep dependency, application, supervision, endpoint, router, migration, and
  config changes intentional.
- Prefer standard library, OTP, and existing project helpers before adding deps.

For Mix projects, umbrella apps, dependencies, formatter/Credo/Dialyzer,
config, releases, and CI: `get_skill_file(name="elixir", path="references/configuration.md")`

## Types And Contracts

- Treat public modules, behaviours, callbacks, structs, schemas, contexts,
  protocol implementations, and API boundaries as contracts.
- Use pattern matching, guards, structs, typespecs, opaque types, behaviours,
  and clear return shapes to make data and failure modes explicit.
- Preserve compatible return tuples, message formats, schema fields, and
  callback contracts unless the change is an intentional API break.

For module contracts, typespecs, structs, behaviours, protocols, schemas, and
return shapes: `get_skill_file(name="elixir", path="references/types-and-contracts.md")`

## OTP And Concurrency

- Model process ownership, supervision strategy, restart behavior, message
  protocols, timeouts, cancellation, and backpressure before adding processes.
- Prefer supervised GenServer, Supervisor, Task.Supervisor, DynamicSupervisor,
  Registry, Broadway/Oban, and existing process abstractions over ad hoc spawn.
- Keep process state small, serializable, observable, and recoverable.

For GenServer, Supervisor, Task, Registry, messaging, timeouts, and process
state: `get_skill_file(name="elixir", path="references/otp-and-concurrency.md")`

## Data And Boundaries

- Keep Phoenix controllers, LiveViews, contexts, Ecto schemas, changesets,
  queries, external clients, and domain modules separated by clear boundaries.
- Validate and normalize input at edges, keep domain operations explicit, and
  avoid hiding database, HTTP, filesystem, or process side effects in callbacks.
- Preserve transaction, idempotency, migration, and context boundaries.

For Phoenix, LiveView, Ecto, contexts, external services, migrations, and
boundary design: `get_skill_file(name="elixir", path="references/data-and-boundaries.md")`

## Errors And Observability

- Follow local conventions for `{:ok, value}` / `{:error, reason}`, `with`,
  exceptions, `Ecto.Multi`, retries, logging, and user-facing errors.
- Preserve original error context and avoid converting structured failures into
  vague atoms or strings.
- Emit Logger, Telemetry, metrics, spans, and audit events where the surrounding
  code already does so.

For result tuples, exceptions, `with`, retries, Logger, Telemetry, and audit
events: `get_skill_file(name="elixir", path="references/errors-and-observability.md")`

## Testing

- Add focused ExUnit coverage for changed modules, behaviours, process messages,
  supervision failures, Ecto changesets/queries, Phoenix boundaries, and
  external-service edges.
- Use the repo's test stack: ExUnit, async tests, SQL sandbox, Mox, Bypass,
  StreamData, Wallaby, Phoenix.ConnTest, LiveViewTest, or custom fixtures.
- Prefer deterministic unit and boundary tests before broad umbrella runs.

For ExUnit, async safety, DB sandboxing, mocks, property tests, Phoenix tests,
and commands: `get_skill_file(name="elixir", path="references/testing.md")`

## Performance And Releases

- Measure hot paths before optimizing. Check reductions, mailbox growth,
  scheduler pressure, ETS, binary retention, N+1 queries, streams, and memory.
- Keep releases, config providers, runtime secrets, clustering, migrations, and
  startup/shutdown behavior compatible with deployment.
- Use ETS, persistent_term, pooling, batching, or process topology changes only
  with evidence and tests.

For BEAM performance, profiling, ETS, binaries, releases, runtime config, and
deployment: `get_skill_file(name="elixir", path="references/performance-and-releases.md")`

## Before You Finish

If you touched Elixir: verify formatting, focused analysis, compile, relevant
tests, and any configured release/runtime checks pass before closing your work.
