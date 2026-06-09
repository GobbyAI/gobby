# PHP Performance

Use this reference when changing hot paths, request handlers, database access,
serialization, queues, batch jobs, caches, filesystem work, or memory-heavy
transforms.

## Evidence First

- Profile before optimizing with repo-approved metrics, Blackfire, Xdebug,
  Tideways, query plans, logs, or focused benchmarks.
- Keep benchmark input sizes close to production behavior.
- Do not trade validation, security, or clear contracts for speculative speed.
- Record before/after measurements when performance is the purpose of the
  change.

## Memory And Data Flow

- Avoid loading unbounded result sets, files, or decoded payloads into memory.
- Use generators, iterators, pagination, chunking, streaming responses, and
  cursors when data can grow.
- Avoid repeated reflection, container lookups, regex compilation, and
  serialization work in hot loops.
- Watch copy-on-write costs when passing and modifying large arrays.

## Database And ORM

- Look for N+1 queries, accidental eager loading, missing indexes, broad
  transactions, and slow serialization of entity graphs.
- Batch writes and reads where consistency rules allow.
- Use query plans or framework query logs to prove database changes.
- Keep migrations and query changes compatible with production data volume.

## Caching

- Define cache keys, TTLs, invalidation, serialization format, stampede
  behavior, and tenant/user scoping.
- Avoid hidden global caches unless lifecycle and tests are clear.
- Test stale, missing, and failed cache behavior.

## Runtime

- Respect opcache, preload, autoloader optimization, worker lifecycle, and memory
  limit assumptions.
- In long-running PHP runtimes such as Swoole, RoadRunner, or queue workers,
  avoid request-scoped state leaking across jobs or requests.
