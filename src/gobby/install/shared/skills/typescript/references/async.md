# Async - Reference

Source note: adapted for Gobby using CUBETIQ TypeScript patterns plus SkillsMP async and JavaScript strictness checks.

## Promise Ownership

Every promise needs an owner:

```ts
await sendEmail(message);
return saveUser(user);
void startBackgroundRefresh().catch((error) => {
  logger.error("background_refresh_failed", { error });
});
```

Use `void` only when the promise is intentionally detached and has its own error handling. Type-aware ESLint should flag accidental floating promises.

## Cancellation

Accept `AbortSignal` for async work that can outlive the caller:

```ts
async function fetchJson(url: string, signal: AbortSignal): Promise<unknown> {
  const response = await fetch(url, { signal });
  return response.json();
}
```

Pass the signal through every layer that performs I/O. Do not swallow abort errors unless the caller explicitly treats cancellation as success.

## Timeouts

Wrap operations with explicit deadlines:

```ts
async function withTimeout<T>(
  operation: (signal: AbortSignal) => Promise<T>,
  timeoutMs: number,
): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await operation(controller.signal);
  } finally {
    clearTimeout(timeout);
  }
}
```

Prefer caller-provided timeouts at boundary layers so retry and cancellation policy stays visible.

## Bounded Concurrency

Avoid unbounded `Promise.all(items.map(...))` for large or external workloads. Use a pool or limiter:

```ts
async function mapLimit<T, R>(
  items: readonly T[],
  limit: number,
  worker: (item: T) => Promise<R>,
): Promise<R[]> {
  const results: R[] = [];
  let next = 0;

  async function run(): Promise<void> {
    while (next < items.length) {
      const index = next;
      next += 1;
      const item = items[index];
      results[index] = await worker(item);
    }
  }

  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, run));
  return results;
}
```

Use an existing library if the repo already has one. Keep ordering guarantees explicit.

## Error Handling In Async Flows

Use `Promise.all` when all tasks must succeed. Use `Promise.allSettled` when partial success is expected:

```ts
const settled = await Promise.allSettled(tasks.map(runTask));
const failures = settled.filter((result) => result.status === "rejected");
```

Convert settled results to typed domain results before returning them from public APIs.

## Retries

Retry only idempotent or explicitly safe operations:

```ts
async function retry<T>(attempt: () => Promise<T>, maxAttempts: number): Promise<T> {
  let lastError: unknown;
  for (let i = 0; i < maxAttempts; i += 1) {
    try {
      return await attempt();
    } catch (error: unknown) {
      lastError = error;
    }
  }
  throw new Error("Retry attempts exhausted", { cause: lastError });
}
```

Add backoff and jitter for real network calls. Stop retrying on validation, authorization, or non-retriable status errors.

## UI And Server Boundaries

- In UI code, clean up async effects and ignore stale responses with aborts or sequence IDs.
- In server code, propagate request cancellation to database, HTTP, and queue clients when supported.
- In jobs, record idempotency keys before side effects.
- In CLIs, handle `SIGINT` and cleanup temporary files or child processes.

## Async Tests

Test timeouts, cancellation, and partial failure explicitly. Use fake timers where supported. Avoid real sleeps because they make suites slow and flaky.
