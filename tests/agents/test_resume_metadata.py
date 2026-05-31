"""Tests for agent resume metadata serialization helpers."""

from __future__ import annotations

import pytest

from gobby.agents.resume_metadata import json_safe

pytestmark = pytest.mark.unit


def test_json_safe_sorts_set_values() -> None:
    assert json_safe({"beta", "alpha"}) == ["alpha", "beta"]
    assert json_safe({("beta", 2), ("alpha", 1)}) == [["alpha", 1], ["beta", 2]]
