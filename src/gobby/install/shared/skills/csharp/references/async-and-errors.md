# C# Async And Error Handling

Make asynchronous work, cancellation, and failure modes explicit.

## Async APIs

- Use `async`/`await` for I/O. Avoid sync-over-async calls such as `.Result`,
  `.Wait()`, and `GetAwaiter().GetResult()`.
- Accept `CancellationToken` on public async operations that can block on I/O,
  locks, queues, network calls, or database work.
- Pass cancellation tokens through to EF Core, HTTP, streams, channels, queues,
  and file APIs where supported.
- Use `ValueTask` only for hot paths where measurements justify the complexity.
- Avoid fire-and-forget work unless it is handed to a durable queue or hosted
  service with logging, cancellation, and retry policy.

## Streams And Background Work

- Dispose `IAsyncDisposable` resources with `await using`.
- Keep `IAsyncEnumerable<T>` lazy, cancellation-aware, and single-enumeration
  safe unless documented otherwise.
- Bound channels, queues, and parallelism. Unbounded background work becomes a
  production outage under load.
- Do not capture scoped services in singleton/background services; create scopes
  intentionally.

## Exceptions

- Catch specific exceptions where you can add context or translate the failure.
- Use `throw;` to rethrow while preserving stack traces.
- Wrap third-party exceptions in domain/application exceptions at boundaries.
- Do not catch `Exception` inside core logic to return default data.
- Include actionable context in exception messages without leaking secrets.

## Expected Failures

- Use result types, validation results, or problem details for expected user,
  domain, and concurrency failures.
- Use exceptions for exceptional infrastructure or invariant failures.
- Map database concurrency, duplicate-key, timeout, auth, validation, parsing,
  and external service failures at the adapter layer.

## Timeouts And Retries

- Set explicit timeouts for HTTP, database, queue, and external service calls.
- Put retries at the edge and make them idempotent.
- Do not retry validation failures, authorization failures, malformed payloads,
  or unknown side-effecting operations.
