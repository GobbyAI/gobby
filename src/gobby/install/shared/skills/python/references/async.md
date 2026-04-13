# Async — Reference

## TaskGroup (Python 3.11+)

Prefer `TaskGroup` over manual `create_task()` — it handles cancellation and error propagation:

```python
async def fetch_all(urls: list[str]) -> list[Response]:
    results: list[Response] = []
    async with asyncio.TaskGroup() as tg:
        for url in urls:
            tg.create_task(fetch_one(url, results))
    return results
```

If any task raises, `TaskGroup` cancels the remaining tasks and propagates as `ExceptionGroup`.

## Concurrent I/O with gather

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

## Rate Limiting with Semaphore

```python
sem = asyncio.Semaphore(10)  # max 10 concurrent

async def rate_limited_fetch(url: str) -> Response:
    async with sem:
        return await client.get(url)
```

## Timeouts

```python
try:
    result = await asyncio.wait_for(slow_operation(), timeout=30.0)
except TimeoutError:
    logger.warning("Operation timed out after 30s")
```

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

## Offloading CPU Work

```python
import asyncio
from concurrent.futures import ProcessPoolExecutor

executor = ProcessPoolExecutor(max_workers=4)

async def compute_heavy(data: bytes) -> Result:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, cpu_bound_fn, data)
```
