"""Tests for the backend-neutral hub database protocol."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Literal, get_args, get_origin, get_type_hints

from gobby.storage.hub.protocol import HubDatabase, LockAcquisitionOrderError, LockTarget, Row


def test_protocol_symbols_are_importable() -> None:
    assert issubclass(LockAcquisitionOrderError, RuntimeError)
    assert HubDatabase.__module__ == "gobby.storage.hub.protocol"
    assert LockTarget.__module__ == "gobby.storage.hub.protocol"


def test_row_alias_is_backend_neutral_mapping() -> None:
    assert get_origin(Row) is Mapping
    assert get_args(Row) == (str, Any)


def test_lock_target_priority_contract_is_class_level() -> None:
    hints = get_type_hints(LockTarget, include_extras=True)

    assert hints["PRIORITY"] == ClassVar[int]


def test_hub_database_dialect_contract_names_supported_backends() -> None:
    hints = get_type_hints(HubDatabase, include_extras=True)

    assert hints["dialect"] == Literal["sqlite", "postgres"]
