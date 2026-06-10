# Concurrency And Error Handling

Use this reference when editing async functions, actors, tasks, task groups,
MainActor code, Sendable models, retries, resources, or exception translation.

## Structured Concurrency

- Prefer `async let`, `withTaskGroup`, `withThrowingTaskGroup`, actor methods,
  framework-owned tasks, and injected async services over detached work.
- Avoid `Task.detached` unless the lifecycle, priority, actor isolation, and error
  handling are documented at the call site.
- Bound fan-out with task groups, batches, semaphores, queues, or backpressure.
  Do not start unbounded child tasks from loops.
- Keep actor hops visible. Do not hide MainActor work in helpers that look cheap
  or synchronous.

## Cancellation

- Check cancellation in loops and long-running async operations.
- Propagate `CancellationError` instead of turning cancellation into generic
  failures.
- Use explicit timeouts for network, database, process, file, and external
  service calls.
- Release resources with `defer`, scoped lifetimes, framework hooks, or explicit
  cleanup APIs.

## Sendable And Isolation

- Treat strict concurrency diagnostics as real design feedback. Fix shared state
  with value types, actors, locks, immutability, or dependency boundaries.
- Use `@unchecked Sendable` only when the type has a documented synchronization
  invariant and tests that exercise concurrent use.
- Mark UI-facing models, view models, and UI update APIs with `@MainActor` when
  they own main-thread state.
- Keep non-Sendable framework objects out of detached tasks and cross-actor
  messages unless wrapped safely.

## Error Boundaries

- Use typed domain results for expected business outcomes and thrown errors for
  exceptional failures.
- Preserve underlying causes when translating URLSession, database, file,
  Codable, process, platform, or framework errors.
- Do not collapse errors into vague strings or booleans.
- Log or emit metrics at process, request, job, or UI boundaries, not in tight
  loops unless the event itself matters.

## Testing

- Test success, failure, cancellation, retry, timeout, actor isolation, and
  priority-sensitive paths.
- Prefer deterministic async tests with controlled clocks, fake services, local
  actors, and focused task-group inputs.
