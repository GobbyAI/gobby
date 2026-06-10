# Performance - Reference

## Measure First

Identify the actual bottleneck before changing code:

- CPU profile
- memory profile
- database/query timing
- network latency
- serialization/parsing cost
- event-loop blocking
- import/startup time

Do not trade clarity or type safety for speculative speed.

## Profiling Tools

| Tool | When | Command |
|------|------|---------|
| **cProfile** | Quick CPU profile | `python -m cProfile -s cumtime script.py` |
| **py-spy** | Profile running process | `py-spy record -o flame.svg --pid <PID>` |
| **memory_profiler** | Memory usage | `python -m memory_profiler script.py` |
| **line_profiler** | Line-by-line | `kernprof -l -v script.py` (needs `@profile`) |
| **timeit** | Microbenchmarks | `python -m timeit "expr"` |

## Data Structure Selection

| Need | Use | Not |
|------|-----|-----|
| Membership test | `set` — O(1) | `list` — O(n) |
| Key-value lookup | `dict` — O(1) | Linear scan |
| Ordered unique items | `dict.fromkeys()` | `list` + dedup |
| FIFO queue | `collections.deque` | `list.pop(0)` — O(n) |
| Priority queue | `heapq` | Sorted list |
| Counting | `collections.Counter` | Manual dict |

Avoid repeated linear scans inside loops over large collections. Normalize once, then look up by key.

## Caching

```python
from functools import cache, lru_cache

@cache  # unbounded, Python 3.9+
def fibonacci(n: int) -> int:
    if n < 2:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)

@lru_cache(maxsize=256)  # bounded
def expensive_lookup(key: str) -> Result:
    return database.query(key)
```

Cache pure functions or stable lookups. Do not cache values tied to permissions, request state, environment variables, or external resources without an invalidation plan.

## Generators for Large Data

```python
# Bad: loads entire file into memory
lines = open("huge.log").readlines()
errors = [l for l in lines if "ERROR" in l]

# Good: streams line by line
def error_lines(path: Path) -> Generator[str, None, None]:
    with open(path) as f:
        for line in f:
            if "ERROR" in line:
                yield line
```

Prefer `pathlib.Path.open(encoding="utf-8")` or repo wrappers for file I/O. Stream large JSONL, CSV, logs, and exports instead of loading all rows at once.

## String Building

```python
# Bad: O(n^2) in older Python, still slower
result = ""
for item in items:
    result += str(item)

# Good: O(n)
result = "".join(str(item) for item in items)
```

For repeated regex work, compile patterns once when profiling shows it matters.

## Comprehensions vs Loops

Comprehensions are faster for simple transforms (no function call overhead, C-level loop):

```python
# Prefer
squares = [x**2 for x in range(1000)]
evens = {x for x in data if x % 2 == 0}
mapping = {k: v.strip() for k, v in pairs}

# But don't nest 3+ deep — use a loop for readability
```

Use explicit loops when there is branching, error handling, logging, or non-trivial transformation.

## I/O And Database Loops

Most Python "performance" bugs are boundary bugs:

- batch database writes instead of one query per row
- page large reads and stream responses
- avoid unbounded concurrent network calls
- reuse clients and connection pools according to their lifecycle rules
- push filtering to the database when it keeps behavior clear

Keep transactions, retries, and idempotency visible.

## Serialization

For parsers and serializers, measure realistic payloads. Avoid repeated parse/dump cycles, repeated schema compilation, and unnecessary conversion between dicts and models in hot paths.

## Benchmarks

Use benchmarks for pure algorithms, parsers, serializers, and transforms. Include representative and worst-case inputs. Keep benchmarks separate from ordinary unit tests unless the repo already has a performance-test convention.
