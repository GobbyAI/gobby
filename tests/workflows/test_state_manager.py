from __future__ import annotations

import logging

import pytest

from gobby.workflows.state_manager import _decode_variables_payload

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
