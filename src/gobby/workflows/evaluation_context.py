"""Detached, read-only inputs at workflow rendering and expression boundaries."""

from dataclasses import replace
from typing import Any, NoReturn

from gobby.hooks.events import HookEvent


def _immutable(*args: Any, **kwargs: Any) -> NoReturn:
    raise TypeError("Workflow evaluation inputs are read-only")


class _ReadOnlyDict(dict[Any, Any]):
    """Preserve dictionary reads, equality and JSON encoding on a detached copy."""

    __setitem__ = __delitem__ = __ior__ = _immutable
    clear = pop = popitem = setdefault = update = _immutable

    def __deepcopy__(self, memo: dict[int, Any]) -> "_ReadOnlyDict":
        return self


class _ReadOnlyList(list[Any]):
    """Preserve list reads and equality without granting mutating methods."""

    __setitem__ = __delitem__ = __iadd__ = __imul__ = _immutable
    append = clear = extend = insert = pop = remove = reverse = sort = _immutable

    def __deepcopy__(self, memo: dict[int, Any]) -> "_ReadOnlyList":
        return self


def freeze_evaluation_value(value: Any) -> Any:
    """Copy JSON state recursively, retaining trusted callables and lazy predicates.

    Dict subclasses with special read semantics (notably unavailable tool input)
    stay opaque; the immutable sandbox still refuses their mutating methods.
    Hook events are copied with detached data and metadata, so event aliases cannot
    expose live nested session or tool state either.
    """
    if isinstance(value, (_ReadOnlyDict, _ReadOnlyList)):
        return value
    if type(value) is dict:
        return _ReadOnlyDict({key: freeze_evaluation_value(item) for key, item in value.items()})
    if type(value) is list:
        return _ReadOnlyList(freeze_evaluation_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(freeze_evaluation_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(freeze_evaluation_value(item) for item in value)
    if isinstance(value, HookEvent):
        return replace(
            value,
            data=freeze_evaluation_value(value.data),
            metadata=freeze_evaluation_value(value.metadata),
        )
    return value


def snapshot_evaluation_context(context: dict[str, Any]) -> dict[str, Any]:
    """Give an evaluator its own local bindings over read-only nested snapshots.

    The top-level dictionary is local and mutable for expression comprehensions;
    every value supplied by the caller is isolated before executing rule code.
    """
    return {key: freeze_evaluation_value(value) for key, value in context.items()}


def mutable_evaluation_result(value: Any) -> Any:
    """Return ordinary detached state when an expression result is persisted."""
    if isinstance(value, dict):
        return {key: mutable_evaluation_result(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mutable_evaluation_result(item) for item in value]
    if isinstance(value, tuple):
        return tuple(mutable_evaluation_result(item) for item in value)
    return value
