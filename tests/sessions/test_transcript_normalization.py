"""Transcript normalization regression tests."""

from __future__ import annotations

import pytest

from gobby.sessions.transcript_normalization import _update_type

pytestmark = pytest.mark.unit


def test_update_type_returns_only_string_values() -> None:
    assert _update_type({"sessionUpdate": "hook:started"}) == "hook:started"
    assert _update_type({"sessionUpdate": 123, "type": "fallback"}) == "fallback"
    assert _update_type({"sessionUpdate": 123, "type": {"name": "bad"}}) == ""
