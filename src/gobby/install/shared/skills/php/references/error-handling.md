# PHP Error Handling

Use this reference when adding exceptions, translating framework/library
failures, handling PHP false-return APIs, working with resources, or validating
external input.

## Exceptions

- Use domain-specific exceptions for exceptional failures callers can distinguish.
- Preserve previous exceptions: `throw new DomainException('context', 0, $e);`.
- Catch broad `Throwable` only at request, CLI, worker, queue, or transaction
  boundaries where logging, rollback, and translation happen.
- Keep exception messages safe for users; log operational context separately.

## False Returns And Warnings

- Many PHP APIs signal failure with `false`, warnings, or partial output. Check
  return values before using them.
- Wrap file, stream, JSON, regex, date/time, image, XML, cURL, and extension
  failures in typed errors at the boundary.
- Use `json_validate` or explicit `json_last_error` handling where the repo's
  PHP version requires it.
- Do not suppress warnings with `@` unless a local wrapper immediately converts
  the result into a checked error.

## Resources And Cleanup

- Close streams, temp files, locks, transactions, and external handles in the
  owner that opened them.
- Prefer framework-managed lifecycles only when tests prove ownership.
- Use `try`/`finally` for cleanup that must run after partial failure.
- Avoid leaving temp files, uploaded files, or lock files behind on exceptions.

## Boundary Translation

- Translate HTTP client, database, filesystem, queue, cache, mail, and framework
  exceptions in adapter code.
- Include operation, resource, status code, or field context; exclude secrets and
  raw sensitive payloads.
- Separate retry/backoff policy from exception construction unless the repo has
  a dedicated resilience layer.

## Validation Failures

- Validate before mutation, persistence, outbound calls, or rendering.
- Return field-specific validation errors where callers need to recover.
- Keep authorization failures distinct from validation and not-found failures.
