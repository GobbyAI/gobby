# Type System — Reference

## TYPE_CHECKING Pattern

```python
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from myapp.models import User

def get_user(user_id: int) -> User: ...
```

## Protocol vs ABC

Use `Protocol` when you only care about structure, not inheritance:

```python
from typing import Protocol

class Serializable(Protocol):
    def to_dict(self) -> dict[str, object]: ...

# Any class with a to_dict method satisfies this — no inheritance needed
```

Use ABC when you need shared implementation or want to enforce registration:

```python
from abc import ABC, abstractmethod

class BaseProcessor(ABC):
    def process(self, data: bytes) -> bytes:
        validated = self.validate(data)
        return self.transform(validated)

    @abstractmethod
    def validate(self, data: bytes) -> bytes: ...

    @abstractmethod
    def transform(self, data: bytes) -> bytes: ...
```

## Narrowing `Any` at Boundaries

When an external library returns `Any`, narrow it immediately:

```python
def get_config_value(key: str) -> str:
    raw: object = external_lib.get(key)  # returns Any
    if not isinstance(raw, str):
        raise TypeError(f"Expected str for {key}, got {type(raw).__name__}")
    return raw
```

## Generic Types

```python
from typing import TypeVar

T = TypeVar("T")

def first_or_none(items: list[T]) -> T | None:
    return items[0] if items else None
```

## TypedDict for Structured Dicts

When you must use dicts (JSON responses, config), type them:

```python
from typing import TypedDict

class UserResponse(TypedDict):
    id: int
    name: str
    email: str | None
```
