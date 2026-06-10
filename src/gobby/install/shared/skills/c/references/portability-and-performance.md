# C Portability And Performance

Correct portable C is already performance work. Measure before making hot-path
code less obvious.

## Portability

- Guard POSIX, Windows, compiler, libc, endian, alignment, and word-size
  assumptions.
- Keep feature-test macros consistent and defined before headers require them.
- Isolate compiler extensions behind macros or small helper functions.
- Treat `char` signedness, `long` width, path encoding, line endings, and locale
  behavior as platform-dependent unless the repo guarantees otherwise.

## Concurrency And Atomics

- Use project threading primitives and locking conventions.
- Document which thread owns each object and which functions are thread-safe.
- Use C atomics only with a clear memory-order reason.
- Avoid mixing atomics, volatile, and locks without a documented invariant.

## Profiling

- Profile representative inputs before changing algorithms, allocation strategy,
  or data layout.
- Measure CPU, allocations, cache misses, syscalls, lock contention, and I/O
  shape as appropriate.
- Keep benchmark fixtures deterministic and close to the changed code.
- Preserve correctness tests alongside benchmarks.

## Hot Paths

- Avoid unnecessary copies, repeated parsing, repeated allocation, and avoidable
  syscalls.
- Use contiguous data, preallocation, lookup tables, or pooling only when the
  workload proves it helps.
- Keep branch hints, SIMD, packed structs, custom allocators, and inline assembly
  behind tests and platform guards.
- Do not trade bounds checks or lifetime clarity for speculative speed.

## Build Modes

- Verify release and sanitizer/debug modes when optimization-sensitive code
  changes.
- Watch for optimizer-dependent undefined behavior. A debug-only pass is weak
  evidence for C changes.
- Keep LTO, PGO, and architecture-specific flags in existing build channels.
