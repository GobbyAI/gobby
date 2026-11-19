"""Parameter and field decoders for op registrations (R0 design 5.12).

Each spec decodes one JSON value. A value that does not match raises ``DecodeError``, and
every decode failure is a ``DecodeError``, including a malformed datetime. A spec that
cannot be built from an annotation raises ``TypeError`` when it is built, which is a
registration error.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import importlib
import types
import typing
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from gobby.worker.encode import WIRE_INT_MAX, WIRE_INT_MIN


class DecodeError(Exception):
    """A JSON value does not match its spec."""


class Spec(Protocol):
    """Decodes one JSON value into the Python value a registration expects."""

    def decode(self, value: object) -> object: ...


class Str:
    def decode(self, value: object) -> str:
        if not isinstance(value, str):
            raise DecodeError("expected a string")
        return value


class Int:
    def decode(self, value: object) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise DecodeError("expected an integer")
        if not WIRE_INT_MIN <= value <= WIRE_INT_MAX:
            raise DecodeError("integer outside the wire range")
        return value


class Bool:
    def decode(self, value: object) -> bool:
        if not isinstance(value, bool):
            raise DecodeError("expected a boolean")
        return value


class Anything:
    def decode(self, value: object) -> object:
        return value


@dataclasses.dataclass(frozen=True)
class Optional:
    """``null`` decodes to ``None``; any other value decodes by ``inner``."""

    inner: Spec

    def decode(self, value: object) -> object:
        if value is None:
            return None
        return self.inner.decode(value)


@dataclasses.dataclass(frozen=True)
class Literal:
    """The value must equal one of ``values`` and have the same JSON type as it."""

    values: tuple[object, ...]

    def decode(self, value: object) -> object:
        for allowed in self.values:
            if type(value) is type(allowed) and value == allowed:
                return value
        raise DecodeError("value is not an allowed literal")


class IsoDatetime:
    def decode(self, value: object) -> datetime:
        if not isinstance(value, str):
            raise DecodeError("expected an ISO 8601 string")
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise DecodeError("not an ISO 8601 datetime") from exc


class JsonObject:
    def decode(self, value: object) -> dict[str, object]:
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise DecodeError("expected a JSON object with string keys")
        return value


# Dataclasses whose field specs are being built. A field that names one of them
# (directly or through another field) keeps its specs unbuilt until the first decode.
_BUILDING: set[type] = set()


class DataclassFields:
    """Decodes a JSON object into a dataclass by its field names and annotations.

    ``target`` is a dataclass class, or a ``"module:Class"`` string resolved on first decode.
    A class target has its field specs built here, so an unsupported annotation fails at
    registration.
    """

    def __init__(self, target: type | str) -> None:
        self._target = target
        self._cls: type | None = target if isinstance(target, type) else None
        self._layout_cache: tuple[dict[str, Spec], frozenset[str]] | None = None
        if isinstance(target, type) and target not in _BUILDING:
            self._layout()

    def decode(self, value: object) -> object:
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise DecodeError("expected a JSON object with string keys")
        specs, required = self._layout()
        unknown = sorted(set(value) - specs.keys())
        if unknown:
            raise DecodeError(f"unknown field(s): {', '.join(unknown)}")
        absent = sorted(required - value.keys())
        if absent:
            raise DecodeError(f"missing required field(s): {', '.join(absent)}")
        decoded = {name: specs[name].decode(item) for name, item in value.items()}
        cls = self._resolve()
        try:
            return cls(**decoded)
        except Exception as exc:
            # The constructor or __post_init__ rejected values that each field accepted. Any
            # Exception is the client's value, not a server fault, so it is invalid_request (R53).
            raise DecodeError(f"constructor rejected the value: {exc}") from exc

    def _resolve(self) -> type:
        if self._cls is None:
            module_name, separator, qualname = str(self._target).partition(":")
            if not separator or not module_name or not qualname:
                raise TypeError(f"dataclass target must be 'module:Class': {self._target!r}")
            try:
                found: object = importlib.import_module(module_name)
            except Exception as exc:
                # A module that fails on import is a registration fault of any class (R53).
                raise TypeError(f"dataclass module does not import: {module_name}: {exc}") from exc
            for part in qualname.split("."):
                found = getattr(found, part, None)
            if not (isinstance(found, type) and dataclasses.is_dataclass(found)):
                raise TypeError(f"dataclass target is not a dataclass: {self._target!r}")
            self._cls = found
        return self._cls

    def _layout(self) -> tuple[dict[str, Spec], frozenset[str]]:
        if self._layout_cache is None:
            cls = self._resolve()
            _BUILDING.add(cls)
            try:
                hints = typing.get_type_hints(cls)
                fields = [field for field in dataclasses.fields(cls) if field.init]
                specs = {field.name: spec_for(hints[field.name]) for field in fields}
                required = frozenset(
                    field.name
                    for field in fields
                    if field.default is dataclasses.MISSING
                    and field.default_factory is dataclasses.MISSING
                )
                self._layout_cache = (specs, required)
            finally:
                _BUILDING.discard(cls)
        return self._layout_cache


def spec_for(annotation: object) -> Spec:
    """Build the spec for a field or parameter annotation (design 5.12).

    Raises ``TypeError`` for an annotation outside the table.
    """
    if annotation is Any:
        return Anything()
    if annotation is str:
        return Str()
    if annotation is bool:
        return Bool()
    if annotation is int:
        return Int()
    if annotation is datetime:
        return IsoDatetime()
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return Literal(typing.get_args(annotation))
    if origin is typing.Union or origin is types.UnionType:
        args = typing.get_args(annotation)
        others = [arg for arg in args if arg is not type(None)]
        if len(args) == 2 and len(others) == 1:
            return Optional(spec_for(others[0]))
        raise TypeError(f"unsupported union annotation: {annotation!r}")
    if annotation in (dict, Mapping) or origin in (dict, collections.abc.Mapping):
        return JsonObject()
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        return DataclassFields(annotation)
    raise TypeError(f"unsupported annotation: {annotation!r}")
