# Type System - Reference

## Baseline Rules

Every function signature should declare parameter and return types. Prefer modern built-in generic syntax:

```python
def normalize_names(values: list[str]) -> dict[str, str]:
    return {value.casefold(): value for value in values}
```

Use `str | None` instead of `Optional[str]` in Python versions that support it. Keep exported functions explicit even when inference inside the function is obvious.

## TYPE_CHECKING Pattern

```python
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from myapp.models import User

def get_user(user_id: int) -> User: ...
```

Use this for imports needed only by the type checker. Do not put imports under `TYPE_CHECKING` when runtime code needs the object for validation, decorators, or subclass checks.

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

## Boundary Types

External data should enter as `object`, `Any`, or a library-specific raw type only at the boundary, then narrow immediately:

```python
def get_config_value(key: str) -> str:
    raw: object = external_lib.get(key)  # returns Any
    if not isinstance(raw, str):
        raise TypeError(f"Expected str for {key}, got {type(raw).__name__}")
    return raw
```

Prefer validation functions, Pydantic models, attrs/dataclasses with validators, or small parser functions over casts. A cast documents belief; a validator proves shape at runtime.

## Dataclasses And Value Objects

Use dataclasses for small immutable domain values:

```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class UserId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.startswith("usr_"):
            raise ValueError(f"Invalid user id: {self.value}")
```

Keep validation at construction boundaries. Avoid passing raw strings for IDs, tokens, paths, or units when mixing them up would be costly.

## Generic Types

```python
from typing import TypeVar

T = TypeVar("T")

def first_or_none(items: list[T]) -> T | None:
    return items[0] if items else None
```

Use generics when they preserve caller-specific types. Avoid abstract generic helpers that make simple code harder to read.

## TypedDict for Structured Dicts

When you must use dicts (JSON responses, config), type them:

```python
from typing import TypedDict

class UserResponse(TypedDict):
    id: int
    name: str
    email: str | None
```

Use `NotRequired` or separate TypedDict variants when optionality matters. For richer behavior or validation, graduate to a dataclass or model.

## Enums And Literals

Use `Literal` for small closed sets used in a few places:

```python
type Status = Literal["pending", "running", "failed", "complete"]
```

Use `Enum` or `StrEnum` when values are public, reused widely, serialized, or need methods.

## Avoiding `Any`

- Do not add `Any` to quiet mypy without explaining the boundary.
- Prefer `object` for values that must be narrowed before use.
- Add local stubs or wrapper functions for third-party APIs with poor typing.
- Keep `cast()` close to the proof that makes it true.

## Public API Surface

Export public types deliberately through package `__init__.py` or documented modules. Do not expose private helper types because tests or downstream code reached into internals.
