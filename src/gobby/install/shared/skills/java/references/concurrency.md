# Java Concurrency

Use this reference before adding threads, executors, schedulers, virtual
threads, futures, locks, reactive code, caches, or shared mutable state.

## Ownership And Lifecycle

- Identify who owns each executor, scheduler, thread, queue, connection, or
  subscription.
- Ensure shutdown happens through `close`, framework lifecycle hooks, test
  cleanup, or application shutdown.
- Do not create unbounded executors or ad hoc threads per request.
- Name thread pools when the repo has a naming convention; logs and diagnostics
  depend on it.

## Cancellation And Interruption

- Propagate cancellation through futures, request contexts, timeouts, reactive
  subscriptions, or framework cancellation signals.
- Restore interruption when catching `InterruptedException` and not rethrowing.
- Avoid blocking calls inside event-loop, servlet container, reactive, or
  scheduler threads unless explicitly designed for that runtime.
- Add timeout tests for operations that wait on external systems.

## Futures And Structured Work

- Keep `CompletableFuture` chains explicit about executor selection.
- Avoid `join()` or `get()` in contexts where blocking can exhaust request or
  event-loop threads.
- Combine failures with context; do not let async wrappers erase the real cause.
- For virtual threads, still bound external resources such as database
  connections, HTTP pools, file descriptors, and rate-limited APIs.

## Shared State

- Prefer immutable data and message passing before locks.
- Use `ConcurrentHashMap`, atomics, locks, or synchronized blocks only when the
  invariants are clear.
- Keep lock scopes small and avoid calling external code while holding locks.
- Document thread-safety on public types that are shared across requests.

## Reactive APIs

- Keep blocking work off event-loop schedulers.
- Test backpressure, cancellation, retry, timeout, and error paths when reactive
  behavior is part of the change.
- Do not mix reactive and blocking styles casually; choose the style the repo's
  runtime is built around.
