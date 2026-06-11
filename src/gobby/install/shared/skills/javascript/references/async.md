# JavaScript Async

Make asynchronous control flow explicit so failures, cancellation, and resource cleanup are predictable.

## Promise Discipline

- Always return or await promises.
- Use `void promise.catch(...)` only for deliberate fire-and-forget work with local error handling.
- Do not mix callback and promise APIs when a promise-native API is available.
- Preserve stack and context when wrapping async failures.

## Cancellation and Timeouts

Use `AbortController` for fetch, long-running I/O, and operations tied to request or component lifetime.

```js
export async function fetchJson(url, { signal, timeoutMs = 10000 } = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  if (signal?.aborted) {
    controller.abort();
  } else {
    signal?.addEventListener("abort", () => controller.abort(), { once: true });
  }

  try {
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok) {
      throw new Error(`Request failed with ${response.status}`);
    }
    return await response.json();
  } finally {
    clearTimeout(timeout);
  }
}
```

## Concurrency

- Limit concurrency for external services, filesystem walks, and large transforms.
- Use `Promise.all` for bounded, independent work that should fail fast.
- Use `Promise.allSettled` when each item must report its own outcome.
- Preserve input order when callers expect it.

## Cleanup

- Close streams, file handles, sockets, and browser resources in `finally`.
- Remove event listeners when the owner is disposed.
- Keep retry loops bounded and cancellable.
- Add jittered backoff only when the external system benefits from retry.

## UI and Browser Work

- Treat component unmount or route changes as cancellation points.
- Avoid updating state after an owner has been disposed.
- Keep one source of truth for pending, fulfilled, empty, and error states.
