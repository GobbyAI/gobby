# Performance And Concurrency

Use this reference when editing hot paths, collection-heavy code, Flow streams,
dispatchers, Android UI work, native/JS targets, or shared mutable state.

## Measure First

- Profile or inspect evidence before optimizing. Use JMH, Android Studio
  profiler, trace tools, allocation tracking, database query logs, or
  production-like measurements when available.
- Identify whether the bottleneck is allocation, boxing, reflection,
  serialization, database access, network I/O, recomposition, dispatcher
  contention, lock contention, or platform startup.

## Allocation And Collections

- Avoid speculative rewrites between loops, sequences, Flow, and collection
  operators. Pick the form that is clearest unless measurements show a problem.
- Watch accidental copies from data classes, `toList`, `map` chains, spread
  operators, boxing, reflection, and vararg calls in hot loops.
- Use value classes, primitive arrays, persistent collections, caches, or
  streaming only when they improve a measured problem and preserve readability.

## Coroutine And Threading Costs

- Keep dispatcher choice explicit. Avoid hopping dispatchers inside tight loops.
- Bound concurrent work and buffers. Use backpressure rather than accumulating
  unbounded pending work.
- Keep blocking calls out of event loops, Android main thread, and CPU
  dispatchers.
- Protect shared mutable state with confinement, immutability, atomics, mutexes,
  or repo-standard synchronization.

## Android And Compose

- Keep expensive work off the main thread and out of recomposition.
- Preserve stable models, keys, derived state, and remembered values where the
  UI depends on them.
- Measure startup, frame time, memory, and recomposition before changing UI state
  structure for performance.

## KMP Targets

- Check platform-specific costs. Kotlin/Native, JavaScript, Android, and JVM may
  behave differently for reflection, concurrency, collections, and serialization.
- Do not assume a JVM optimization helps Native or JS targets.

## Review Checklist

- What measurement or failing test proves this optimization is needed?
- Does the change preserve cancellation, thread-safety, and lifecycle behavior?
- Are allocations, queries, network calls, and dispatcher hops bounded?
- Is there a rollback path if production measurements disagree?
