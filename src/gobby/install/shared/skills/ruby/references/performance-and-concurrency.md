# Ruby Performance And Concurrency

## Measure First

- Profile before optimizing. Check wall time, allocations, object retention,
  query counts, queue latency, cache hit rate, and memory growth.
- Use local tools where configured: benchmark-ips, StackProf, ruby-prof,
  memory_profiler, rack-mini-profiler, Bullet, Skylight, New Relic, Datadog, or
  custom instrumentation.
- Keep readability unless measurements justify a tradeoff.

## Rails And Data Performance

- Watch for N+1 queries, missing indexes, over-broad eager loading, large
  serialization payloads, unnecessary callbacks, and expensive validations.
- Use `find_each`, batching, pagination, streaming, select lists, and
  preloading intentionally.
- Keep cache keys, invalidation, and race-condition behavior explicit.
- Test query count or explain plans when the repo has helpers for it.

## Runtime Behavior

- Understand the runtime: MRI has a GVL, while JRuby and TruffleRuby have
  different threading behavior.
- Avoid sharing mutable globals, class variables, memoized objects, and singletons
  across threads or requests without synchronization.
- Keep Fiber Scheduler, async HTTP/database clients, connection pools, and
  timeouts compatible.
- Treat Ractors as isolated concurrency with strict shareability rules.

## Background Jobs

- Keep jobs idempotent, retry-safe, and bounded in memory.
- Batch large workloads and release Active Record objects between batches.
- Use queue-specific concurrency, uniqueness, locking, and rate-limit settings
  already established in the repo.
- Preserve shutdown and deployment behavior for long-running jobs.

## Deployment Safety

- Check boot time, eager load, autoloading, initialization side effects, and
  memory before adding global work.
- Avoid loading large files, making network calls, or connecting to external
  services at require time.
- Keep migrations and code compatible across rolling deploys.
