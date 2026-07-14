# Performance And Concurrency

## Measure First

- Use representative data and the repository's profiler or benchmark harness.
  JMH is the usual JVM microbenchmark tool; keep warmup, forks, and result
  consumption valid.
- Profile end-to-end latency and allocation before replacing readable code with
  a lower-level collection, loop, macro, cache, or concurrency mechanism.
- Check every deployed target. JVM, Scala.js, Scala Native, and distributed
  runtimes have different allocation and scheduling costs.

## Allocation And Collections

- Review boxing at generic, trait, varargs, array, and Java interop boundaries.
  Opaque types and value classes do not guarantee zero allocation across every
  boundary.
- Choose strict collections, iterators, views, lazy lists, and streams according
  to traversal count, memory, short-circuiting, and effect semantics.
- Avoid repeated conversions and intermediate collections on measured hot paths.
  Preserve ordering, laziness, and failure behavior while optimizing.
- Bound caches and queues and define eviction, ownership, and observability.

## Concurrency

- Use the concurrency model already selected by the module: executor/Future,
  effect fibers, actors, streams, or platform primitives.
- Keep blocking work on a bounded blocking pool or runtime facility. Do not block
  event loops, actor dispatchers, global execution contexts, or compute pools.
- Prefer immutable messages and runtime-native `Ref`, queue, semaphore, promise,
  or actor state over unsynchronized shared mutation.
- Define cancellation, backpressure, fairness, timeout, and shutdown behavior.
  Exercise race-sensitive paths repeatedly with deterministic controls where
  possible.

## Performance Evidence

Record the baseline, changed result, workload, runtime/toolchain, and variance.
Keep a regression test or benchmark when the performance characteristic is part
of the contract.
