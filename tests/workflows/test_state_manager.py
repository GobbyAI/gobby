from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from gobby.workflows.state_manager import (
    _LIVE_VARIABLE_MANAGERS,
    SessionVariableManager,
    _clear_variable_defaults_caches,
    _decode_variables_payload,
)

pytestmark = pytest.mark.unit


def test_decode_variables_payload_returns_empty_for_malformed_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    assert _decode_variables_payload("{bad") == {}

    assert "Failed to decode workflow variables payload" in caplog.text


def test_decode_variables_payload_returns_empty_for_non_object_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    assert _decode_variables_payload('["not", "an", "object"]') == {}

    assert "Ignoring non-object workflow variables payload: list" in caplog.text


def test_session_variable_manager_registers_under_cache_lock() -> None:
    manager = SessionVariableManager(MagicMock())
    assert manager in tuple(_LIVE_VARIABLE_MANAGERS)
    _clear_variable_defaults_caches()
    assert manager._defaults_cache == {}
    assert manager._defaults_cache_times == {}
