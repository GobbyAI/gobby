# Ruby Errors And Observability

## Error Shapes

- Follow the repo's convention: exceptions, result objects, dry-monads,
  ActiveModel::Errors, service response objects, or domain-specific error types.
- Raise for exceptional failures. Return explicit results for expected domain
  denials, validation problems, and recoverable branch outcomes when the codebase
  uses that style.
- Preserve original exception context with `raise ...` or wrapper exceptions
  that keep the cause.
- Avoid `rescue nil`, broad `rescue StandardError`, silent retries, and turning
  structured failures into vague strings or symbols.

## Transactions And Retries

- Keep database transactions small and explicit.
- Translate uniqueness, lock, serialization, validation, and timeout failures at
  the boundary that knows the business meaning.
- Make retry behavior idempotent and bounded. Include jitter/backoff where the
  surrounding job/client code uses it.
- Do not rescue `Exception`, `NoMemoryError`, `SystemExit`, or interrupt signals
  outside process boundaries.

## Logging

- Log at request, job, command, and external-service boundaries.
- Include stable identifiers: request ID, job ID, user/account ID, resource ID,
  external correlation ID, and operation name.
- Never log secrets, credentials, session cookies, raw tokens, payment data, or
  unredacted PII.
- Avoid noisy logs inside tight loops or high-volume endpoints.

## Metrics And Tracing

- Use existing instrumentation: ActiveSupport::Notifications, StatsD, OpenTelemetry,
  Prometheus, Honeycomb, Datadog, Sentry, Rollbar, or custom wrappers.
- Emit timing, count, failure, retry, queue latency, and business-event metrics
  where related code already does.
- Preserve tags/cardinality policy. Do not add unbounded user input as metric
  labels.

## User-Facing Errors

- Keep validation messages actionable and stable where tests or clients depend on
  them.
- Do not leak exception messages from database, HTTP clients, shell commands, or
  internal services to users.
- For APIs, preserve status codes, error keys, JSON shape, and localization.
