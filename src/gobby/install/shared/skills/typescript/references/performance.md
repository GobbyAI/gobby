# Performance - Reference

Source note: adapted for Gobby using CUBETIQ TypeScript guidance plus SkillsMP JavaScript strictness and async performance checks.

## Measure First

Do not optimize TypeScript by guesswork. Identify whether the problem is:

- runtime CPU
- memory pressure or object churn
- I/O latency
- bundle size
- render frequency
- TypeScript compiler or editor latency

Use the repo's profiler, browser performance tools, Node `--prof`, framework diagnostics, bundle analyzers, or targeted benchmarks before making invasive changes.

## Runtime Data Structures

Choose data structures that match access patterns:

```ts
const byId = new Map(users.map((user) => [user.id, user]));
const selectedIds = new Set(selection.map((item) => item.id));
```

Use `Map`/`Set` for repeated lookups, arrays for ordered traversal, and plain objects for JSON-like records. Avoid repeated `.find` inside loops over large collections.

## Allocation Discipline

Hot paths should avoid avoidable churn:

- move stable objects and regexes out of loops
- reuse parsed config where safe
- avoid spreading large objects repeatedly in tight loops
- prefer single-pass transforms for large inputs
- stream or chunk large files/responses instead of loading everything at once

Do not sacrifice correctness or clear ownership for micro-allocations outside measured hot paths.

## Async Performance

- Bound concurrency for network, database, filesystem, and CPU-like tasks.
- Deduplicate in-flight requests when many callers ask for the same data.
- Cache only when invalidation rules are clear.
- Prefer cancellation over letting stale async work finish.

```ts
const pending = new Map<string, Promise<User>>();

function loadUser(id: UserId): Promise<User> {
  const existing = pending.get(id);
  if (existing) {
    return existing;
  }
  const promise = fetchUser(id).finally(() => pending.delete(id));
  pending.set(id, promise);
  return promise;
}
```

## Bundle Size

- Use type-only imports for type edges.
- Prefer named imports from libraries that tree-shake well.
- Avoid importing server-only modules into browser bundles.
- Keep package public entrypoints explicit.
- Check bundle reports before replacing dependencies.

`import type` matters because accidental value imports can keep modules in runtime bundles.

## Compiler Performance

The type checker can become the bottleneck. Watch for:

- recursive conditional types over large unions
- template-literal types that generate huge string unions
- wide mapped types over generated API schemas
- heavily generic builder APIs used throughout app code
- large inferred object literals without named interfaces

Mitigations:

- name intermediate types
- cap recursion depth in utility types
- move complex utilities to package boundaries
- use explicit return types on exported functions
- split large generated types by domain
- use project references in monorepos

If a type trick slows every editor action, simplify it. The type system is part of developer runtime.

## React Or UI Hot Paths

When editing UI TypeScript:

- avoid creating unstable callbacks or objects that fan out through memoized children
- keep expensive derived data behind memoization when inputs are stable
- use stable keys and normalized collections
- test render behavior with the framework's tools, not only TypeScript checks

Move to the relevant framework skill for framework-specific performance rules.

## Benchmarks

Use benchmarks for pure algorithms, parsers, serializers, and transforms. Keep fixtures realistic and include worst-case inputs. Do not benchmark code that spends most time in network or database calls without replacing those dependencies with controlled fakes.
