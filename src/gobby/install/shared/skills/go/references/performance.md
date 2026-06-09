# Go Performance Reference

Optimize from measurements. Go makes allocation and CPU behavior visible enough that speculative rewrites usually show up as noise.

## Measurement

- Use `go test -bench=. -benchmem ./path` for focused benchmarks.
- Use `pprof` for CPU, memory, goroutine, and block profiles on real workloads.
- Keep before and after benchmark output in the task or PR when performance is the stated reason.
- Use `go test -run '^$' -bench <Name> -count 5` when comparing small effects.

## Allocation

- Preallocate slices and maps when sizes are known.
- Reuse buffers only when ownership and lifetime are obvious.
- Avoid converting between `string` and `[]byte` in tight loops unless necessary.
- Prefer streaming through `io.Reader` and `io.Writer` for large data.

## Hot Paths

- Keep JSON, parsing, serialization, and logging paths allocation-aware.
- Avoid reflection in hot loops unless a library already owns the cost.
- Use `sync.Pool` only for high-churn temporary objects and never for correctness.
- Keep lock scopes small and measure contention before introducing lock-free structures.

## Dependency Cost

New dependencies add compile time, binary size, transitive risk, and maintenance surface. For performance-sensitive work, verify that the dependency improves the measured path and does not make cold-start or memory behavior worse.
