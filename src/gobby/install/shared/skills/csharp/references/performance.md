# C# Performance

Measure first, then optimize the constrained path.

## What To Measure

- Allocations, large object heap pressure, boxing, closures, and LINQ
  materialization.
- Database query count, query shape, indexes, transaction scope, and result size.
- Serialization cost, payload size, compression, and source-generator use.
- Async overhead, thread-pool starvation, lock contention, and queue backlogs.
- Logging and metrics overhead on hot paths.

## Runtime Tools

- Use BenchmarkDotNet for microbenchmarks when a small operation is the target.
- Use `dotnet-counters`, `dotnet-trace`, `dotnet-gcdump`, or profiler tooling for
  runtime behavior.
- Use database explain plans and provider logs for query work.
- Compare before/after numbers and keep benchmarks near the code when useful.

## Allocation Control

- Avoid repeated LINQ enumeration on hot paths.
- Prefer `Array.Empty<T>()`, pooled buffers, and reused immutable values where
  measurements show allocation pressure.
- Use `Span<T>`, `Memory<T>`, `ReadOnlySpan<T>`, and pipelines only when their
  lifetime rules are clear and tests cover edge cases.
- Do not introduce unsafe code without a narrow, measured reason.

## Data Access

- Project only needed columns.
- Page large result sets and stream where appropriate.
- Use compiled queries or caching only after proving query compilation or
  repeated lookup cost matters.
- Watch N+1 queries, accidental client evaluation, and loading full aggregates
  to update one field.

## Async And Concurrency

- Bound parallelism with channels, semaphores, dataflow, or worker pools.
- Do not use `Task.Run` to hide blocking I/O on server request paths.
- Use cancellation and timeouts to free resources under load.
- Keep locks small and avoid blocking locks around async work.
