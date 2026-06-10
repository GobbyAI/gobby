# C++ Concurrency

Use this reference when editing threads, task queues, coroutines, atomics,
callbacks, locks, or shared mutable state.

## Ownership And Thread Affinity

- Identify which thread owns each object and which operations are thread-safe.
- Avoid detached threads and hidden global executors. Prefer scoped threads,
  RAII task handles, bounded pools, and explicit shutdown.
- Do not capture raw `this` into async work unless the object's lifetime is
  guaranteed.

## Locks And Shared State

- Prefer immutable data sharing, message passing, or narrow critical sections
  over broad locks.
- Use scoped lock types and document lock ordering when multiple locks can be
  held together.
- Never call unknown callbacks while holding locks unless the surrounding design
  explicitly permits reentrancy.

## Atomics

- Use mutexes before atomics unless lock-free behavior is required and tested.
- When atomics are necessary, document memory ordering. Default to the simplest
  ordering that preserves correctness.
- Do not mix atomic and non-atomic access to the same state.

## Coroutines And Async APIs

- Make cancellation, lifetime, executor/thread resumption, and error propagation
  explicit.
- Keep awaitable objects alive through suspension points.
- Validate that exceptions, cancellation, and cleanup behave correctly across
  coroutine boundaries.

## Testing Races

- Run ThreadSanitizer or the repo's race-testing tool for concurrency changes
  when available.
- Add deterministic tests for cancellation, shutdown, timeouts, queue bounds,
  lock ordering, and callback lifetime.
- Avoid sleep-based tests when a condition variable, fake clock, or explicit
  synchronization point can make the test deterministic.
