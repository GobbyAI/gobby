# Elixir Errors And Observability

## Error Shapes

- Follow local conventions for `{:ok, value}`, `{:error, reason}`, exceptions,
  tagged tuples, changeset errors, and `Ecto.Multi` failures.
- Preserve structured reason data across boundaries. Avoid collapsing errors to
  `:error`, `:failed`, or plain strings when callers need context.
- Use `with` for linear result flows only when each branch has clear error
  handling.

## Exceptions

- Reserve exceptions for exceptional or boundary-level failures according to
  local style.
- Do not rescue broadly and continue with partial state.
- When rescuing, preserve stack/context through logging, telemetry, or a
  structured error.

## Logging And Telemetry

- Match existing Logger metadata, levels, redaction, and correlation IDs.
- Emit Telemetry events where surrounding code observes similar operations.
- Do not log secrets, tokens, passwords, raw PII, or full external payloads.
- Keep logs useful for operators: operation, key IDs, timing, outcome, and
  reason category.

## Retries And Compensation

- Make retry policies explicit and bounded.
- Keep retries idempotent or guarded by idempotency keys.
- For transactions and workflows, define rollback or compensation behavior
  before adding side effects.
