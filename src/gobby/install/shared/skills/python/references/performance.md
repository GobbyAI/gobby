# Performance — Reference

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

## String Building

```python
# Bad: O(n^2) in older Python, still slower
result = ""
for item in items:
    result += str(item)

# Good: O(n)
result = "".join(str(item) for item in items)
```

## Comprehensions vs Loops

Comprehensions are faster for simple transforms (no function call overhead, C-level loop):

```python
# Prefer
squares = [x**2 for x in range(1000)]
evens = {x for x in data if x % 2 == 0}
mapping = {k: v.strip() for k, v in pairs}

# But don't nest 3+ deep — use a loop for readability
```
