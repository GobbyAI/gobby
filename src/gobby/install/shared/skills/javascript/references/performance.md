# JavaScript Performance

Optimize from evidence. JavaScript performance depends on runtime, data shape, bundle size, and I/O behavior.

## Measurement

- Reproduce the slow path before changing code.
- Use browser performance tools, Node profiling, test runner timing, or lightweight instrumentation.
- Keep before/after numbers when the optimization is non-obvious.
- Do not trade correctness or readable contracts for speculative speed.

## Runtime Hot Paths

- Avoid avoidable allocation in render loops, parsers, serializers, and large transforms.
- Prefer `Map` and `Set` for repeated lookups over linear scans.
- Keep object shapes stable in hot loops.
- Avoid repeated regular-expression compilation inside loops.
- Stream large files or responses instead of buffering everything when the caller can consume chunks.

## Bundles and Dependencies

- Check whether a new dependency ships ESM, tree-shakes cleanly, and fits the runtime target.
- Prefer platform APIs for small utilities when compatibility allows.
- Avoid importing all of a large library for one helper.
- Keep server-only code out of browser bundles.

## DOM and UI

- Batch DOM writes and reads to avoid layout thrashing.
- Use stable keys and memoization where render churn is measured.
- Debounce or throttle user-driven high-frequency work such as search, resize, scroll, and pointer movement.
- Clean up observers, intervals, and event listeners.

## Data Processing

- Normalize once at boundaries instead of repeatedly adapting the same payload.
- Use iterative transforms for very deep or large inputs.
- Keep caches bounded or tied to lifecycle owners.
- Document cache invalidation rules when stale data would be user-visible.
