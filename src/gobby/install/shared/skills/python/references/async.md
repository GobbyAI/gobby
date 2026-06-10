# Async - Reference

## Choose Async Deliberately

Use `async def` when the work is I/O-bound and the libraries are async-aware. Keep CPU-heavy work synchronous or move it to an executor/process pool. Do not call blocking HTTP, database, subprocess, or filesystem clients directly inside the event loop.

## TaskGroup (Python 3.11+)

Prefer `TaskGroup` over manual `create_task()` — it handles cancellation and error propagation:

```python
async def fetch_all(urls: list[str]) -> list[bytes]:
    results: list[bytes] = []
    async with asyncio.TaskGroup() as tg:
        for url in urls:
            tg.create_task(fetch_one(url, results))
    return results
```

If any task raises, `TaskGroup` cancels the remaining tasks and propagates as `ExceptionGroup`.

## Concurrent I/O With gather

When you don't need lifecycle management:

```python
a, b, c = await asyncio.gather(
    fetch_users(),
    fetch_orders(),
    fetch_inventory(),
)
```

Use `return_exceptions=True` when partial failure is acceptable:

```python
results = await asyncio.gather(*tasks, return_exceptions=True)
for r in results:
    if isinstance(r, Exception):
        logger.error("Task failed", exc_info=r)
```

Convert partial failures to typed results before returning from a public API.

## Rate Limiting with Semaphore

```python
sem = asyncio.Semaphore(10)  # max 10 concurrent

async def rate_limited_fetch(url: str) -> Response:
    async with sem:
        return await client.get(url)
```

Use a queue or worker pool when work arrives continuously. Keep ordering guarantees explicit.

## Timeouts

```python
try:
    result = await asyncio.wait_for(slow_operation(), timeout=30.0)
except TimeoutError:
    logger.warning("Operation timed out after 30s")
```

Prefer `asyncio.timeout()` for scoped timeout blocks in modern Python:

```python
async with asyncio.timeout(30):
    return await client.fetch_user(user_id)
```

Set timeouts at the boundary that understands retry and cancellation policy.

## Cancellation Handling

```python
async def long_running_worker() -> None:
    try:
        while True:
            await process_next_item()
    except asyncio.CancelledError:
        await cleanup()  # flush buffers, close connections
        raise  # always re-raise CancelledError
```

Do not convert cancellation into success unless the caller explicitly defines that behavior.

## Offloading CPU Work

```python
import asyncio
from concurrent.futures import ProcessPoolExecutor

executor = ProcessPoolExecutor(max_workers=4)

async def compute_heavy(data: bytes) -> Result:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, cpu_bound_fn, data)
```

Use `asyncio.to_thread()` for small blocking calls when a thread is acceptable:

```python
content = await asyncio.to_thread(path.read_text, encoding="utf-8")
```

Prefer async-native libraries for high-volume I/O.

## Background Tasks

Detached tasks need explicit ownership:

```python
task = asyncio.create_task(refresh_cache())
task.add_done_callback(log_background_failure)
```

Store the task, handle failures, and cancel it during shutdown. Do not create untracked tasks from request handlers.

## Async Tests

Use pytest's configured async plugin. Test cancellation, timeout, retries, and partial failure with controlled tasks or fake timers. Avoid real sleeps; they make tests flaky and slow.
