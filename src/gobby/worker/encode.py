"""Canonical JSON encoder for worker results (R0 design 5.3, 5.11).

``encode_value`` turns an op's return value into JSON-compatible data by the rules of
design 5.11, and refuses any shape outside them with ``UnencodableValue``.
``canonical_bytes`` serializes JSON-compatible data with the canonical settings. It does
not encode a second time: an encoded dataclass or exception carries a ``_type`` key, which
encoding refuses, so the caller passes the output of ``encode_value`` straight through.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import math
from collections.abc import Mapping
from datetime import datetime
from pathlib import PurePath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


class UnencodableValue(Exception):
    """A value outside the design 5.11 shapes. The op's call fails as ``op_failed``."""


def encode_value(value: object) -> object:
    """Return ``value`` as JSON-compatible data, or raise ``UnencodableValue``."""
    if isinstance(value, enum.Enum):
        return encode_value(value.value)
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise UnencodableValue("non-finite float")
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _encode_dataclass(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, set | frozenset):
        return sorted((encode_value(item) for item in value), key=_canonical_text)
    if isinstance(value, list | tuple):
        return [encode_value(item) for item in value]
    if isinstance(value, dict):
        return _encode_mapping(value)
    if isinstance(value, Exception):
        return _encode_exception(value)
    raise UnencodableValue(f"unencodable value of type {type(value).__name__}")


def canonical_bytes(payload: object) -> bytes:
    """Serialize JSON-compatible ``payload`` as canonical UTF-8 bytes (design 5.3)."""
    try:
        return _canonical_text(payload).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise UnencodableValue("payload is not canonical JSON") from exc


def _canonical_text(payload: object) -> str:
    return json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )


def _encode_mapping(mapping: Mapping[Any, Any]) -> dict[str, object]:
    encoded: dict[str, object] = {}
    for key, item in mapping.items():
        if not isinstance(key, str):
            raise UnencodableValue("mapping key is not a string")
        if key == "_type":
            raise UnencodableValue("mapping key collides with the _type tag")
        encoded[key] = encode_value(item)
    return encoded


def _encode_dataclass(instance: DataclassInstance) -> dict[str, object]:
    if any(field.name == "_type" for field in dataclasses.fields(instance)):
        raise UnencodableValue("dataclass field collides with the _type tag")
    try:
        plain = dataclasses.asdict(instance)
    except Exception as exc:
        raise UnencodableValue("dataclass field could not be copied") from exc
    encoded = _encode_mapping(plain)
    encoded["_type"] = type(instance).__name__
    return encoded


def _encode_exception(exc: Exception) -> dict[str, object]:
    name = type(exc).__name__
    try:
        attributes = vars(exc)
    except TypeError:
        return {"_type": name}
    encoded = _encode_mapping(attributes)
    encoded["_type"] = name
    return encoded
