# Coroutines And Error Handling

Use this reference when editing suspend functions, Flow, channels, jobs,
dispatchers, retries, resources, or exception translation.

## Structured Concurrency

- Prefer `coroutineScope`, `supervisorScope`, injected scopes, ViewModel scopes,
  lifecycle scopes, or framework-owned scopes over `GlobalScope`.
- Do not launch work whose lifecycle, cancellation owner, and failure behavior
  are unclear.
- Bound fan-out with semaphores, worker pools, buffers, or batching. Avoid
  starting unbounded child jobs from loops.
- Inject dispatchers where tests or platform boundaries need control. Do not hide
  blocking I/O on `Dispatchers.Default` or CPU work on `Dispatchers.IO`.

## Cancellation

- Never swallow `CancellationException`. Re-throw it after cleanup.
- Use explicit timeouts for network, database, process, and external calls.
- Make retry loops cancellation-aware and bounded.
- Close resources in `use`, `try/finally`, or framework lifecycle hooks.

## Flow And Channels

- Choose cold Flow, hot SharedFlow/StateFlow, Channel, callbackFlow, or suspend
  return values based on ownership and backpressure needs.
- Make buffering, replay, conflation, dispatcher changes, and exception handling
  visible.
- Close callbackFlow resources with `awaitClose` and test cancellation paths.
- Avoid collecting Flow forever in domain code without a lifecycle owner.

## Error Boundaries

- Use exceptions for exceptional failures and typed domain results for expected
  business outcomes.
- Preserve causes when translating HTTP, database, file, serialization,
  platform, or framework exceptions.
- Do not collapse structured errors into vague strings or booleans.
- Log or emit metrics at boundaries, not inside tight coroutine loops unless the
  event itself is meaningful.

## Testing

- Use `runTest`, `StandardTestDispatcher`, fake clocks, Turbine, and deterministic
  fakes for coroutine timing.
- Test cancellation, timeout, retry, dispatcher, and failure paths, not just the
  happy path.
