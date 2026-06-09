# Java Performance

Use this reference when performance is part of the change or when code touches a
known hot path, parser, serializer, collection-heavy transform, cache, network
adapter, database path, or concurrency primitive.

## Evidence First

- Profile before optimizing with repo-approved tooling, JFR, async-profiler,
  application metrics, database query plans, or JMH.
- Keep benchmark input sizes and assumptions close to production behavior.
- Do not trade correctness, type-safety, or clear APIs for speculative speed.
- Record before/after measurements when performance is the reason for the
  change.

## Allocation And Collections

- Avoid accidental quadratic loops in nested collection work.
- Pre-size collections only when sizes are known and the code is hot enough to
  justify it.
- Prefer primitive-specialized structures only when profiling shows boxing cost.
- Be deliberate with streams in hot paths; streams are fine for clarity, but do
  not assume they are faster than loops.

## I/O And Serialization

- Stream large payloads instead of buffering whole files or responses when the
  API allows it.
- Reuse configured HTTP, database, serialization, and compression components
  according to repo lifecycle rules.
- Bound timeouts, retries, connection pools, and queue sizes.
- Test malformed and oversized payload behavior for boundary code.

## JVM And GC

- Tune heap, GC, JIT, native memory, or thread settings only with measurements
  and operational rollback clarity.
- Avoid creating many short-lived objects in known hot loops without evidence
  that the allocation rate is acceptable.
- Prefer immutable shared configuration over repeated parsing or reflection.

## Caching

- Define cache key semantics, invalidation, size bounds, TTLs, and concurrency
  behavior.
- Avoid hidden global caches unless the repo already owns lifecycle and testing
  patterns for them.
- Test stale data, eviction, failure, and stampede behavior when cache logic is
  changed.
