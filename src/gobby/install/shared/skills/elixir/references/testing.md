# Elixir Testing

## ExUnit Scope

- Add focused tests for changed modules, contexts, schemas, processes, messages,
  templates, and boundary behavior.
- Prefer `mix test path/to/test.exs` or line-scoped tests while iterating.
- In umbrellas, run tests for the owning app and any changed shared app.

## Async Safety

- Keep `async: true` only when tests do not share mutable state, process names,
  global config, filesystem paths, ports, ETS tables, or database rows.
- Use SQL sandbox ownership correctly for tests that spawn processes.
- Avoid sleeps. Use monitors, messages, telemetry events, deterministic clocks,
  or polling helpers with timeouts.

## Mocks And External Services

- Use repo-approved tools such as Mox, Bypass, test adapters, or local fakes.
- Verify expected calls and failure paths.
- Keep mocks behind behaviours or boundary modules, not scattered through
  domain logic.

## Process And Phoenix Tests

- Test GenServer call/cast/info paths, crash/restart behavior, timeouts, and
  duplicate messages where relevant.
- Use Phoenix.ConnTest, LiveViewTest, component tests, and render assertions for
  request/UI changes.
- Add StreamData/property tests only when they clarify invariants or parsers.

## Validation

- Run formatter, focused tests, compile, and configured analysis before closing.
- Include failing-path tests for result tuples, changesets, retries, and
  supervision behavior affected by the change.
