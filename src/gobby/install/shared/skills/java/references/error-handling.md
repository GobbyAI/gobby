# Java Error Handling

Use this reference when adding exceptions, translating library failures, working
with resources, validating external input, or deciding between exceptions and
typed outcomes.

## Exception Strategy

- Use exceptions for exceptional failures: I/O, protocol violations, persistence
  failure, dependency failure, impossible state, and programmer errors.
- Use typed domain results or explicit state for expected business outcomes:
  validation rejection, permission denial, duplicate entity, or not found when
  that is part of normal flow.
- Preserve root causes with `new DomainException("context", cause)`.
- Do not catch `Exception` broadly except at process, request, job, or framework
  boundary layers where logging and translation happen.
- Avoid swallowing `InterruptedException`; restore interruption with
  `Thread.currentThread().interrupt()` when not rethrowing.

## Checked And Runtime Exceptions

- Follow the repo's existing exception style.
- Checked exceptions can be useful for recoverable boundary failures in library
  APIs, but do not force callers through noisy wrappers for unrecoverable
  failures.
- Runtime exceptions should still be specific and documented at public
  boundaries.
- Keep exception types stable if they are part of a public API.

## Boundary Translation

- Translate HTTP, database, filesystem, queue, serialization, and framework
  exceptions at the boundary where the dependency is known.
- Include useful context such as operation, resource name, status code, or field
  name, but never secrets, tokens, passwords, or raw sensitive payloads.
- Keep retry decisions separate from exception construction unless the repo has
  a dedicated policy object.

## Resources And Cleanup

- Use try-with-resources for `AutoCloseable` values.
- Handle cleanup failures when they matter; do not hide a primary failure with a
  cleanup failure.
- Close streams, clients, transactions, spans, locks, and temp resources in the
  owner that opened them.
- Prefer framework-managed resource lifecycles only when ownership is clear in
  tests and production.

## Validation Failures

- Validate at boundaries before mutation or side effects.
- Return field-specific validation messages or typed errors where callers need
  to recover.
- Keep logs and user-facing messages separate; logs can carry operational
  context, user messages should be stable and safe.
