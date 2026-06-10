# Performance And Memory

Use this reference when changing hot paths, memory ownership, collections,
parsers, rendering loops, actor-heavy paths, or interop code.

## Measure First

- Profile before optimizing. Use Instruments, XCTest performance tests,
  allocation tracking, signposts, package benchmarks, or server metrics.
- Identify whether the problem is CPU, allocation, ARC churn, copying,
  main-thread work, actor hops, I/O, lock contention, or bridging.
- Keep before/after evidence with the task when performance is the reason for
  the change.

## ARC And Lifetimes

- Watch for retain cycles in closures, delegates, notifications, Combine,
  async tasks, timers, and UI callbacks.
- Use weak or unowned captures only when lifetime semantics justify them.
- Keep cleanup visible with `deinit`, cancellation, task ownership, notification
  removal, and explicit close APIs.
- Avoid storing large captured values in long-lived tasks or escaping closures.

## Value Semantics And Collections

- Prefer value types for local immutable data. Check copy-on-write behavior before
  assuming large values are cheap.
- Use arrays, dictionaries, sets, ordered collections, lazy sequences, or custom
  storage based on access patterns.
- Avoid repeated bridging between Swift and Objective-C collections in tight
  loops.
- Keep parser, serializer, and render-loop allocations deliberate.

## Concurrency Costs

- Avoid unnecessary task creation, actor hops, and MainActor round-trips in hot
  paths.
- Batch cross-actor work when possible and keep actor state small.
- Bound parallelism so task groups do not overwhelm CPU, memory, database pools,
  or remote services.
- Measure before replacing clear structured concurrency with lower-level locks or
  unsafe primitives.

## Unsafe And Interop

- Use unsafe pointers, unmanaged references, atomics, and manual memory only when
  platform APIs or measured hot paths require them.
- Document ownership, lifetime, alignment, thread-safety, and aliasing invariants
  near unsafe code.
- Add tests around unsafe wrappers and prefer small audited modules.
