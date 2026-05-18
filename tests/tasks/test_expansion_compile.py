"""Tests for expansion compile normalization helpers."""

from __future__ import annotations

import logging

import pytest

from gobby.tasks.expansion import _compile

pytestmark = pytest.mark.unit


def test_invalid_compiled_priority_logs_raw_and_converted_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="gobby.tasks.expansion._compile")

    priority = _compile._coerce_compiled_task_priority("task-1", "urgent")

    assert priority == 2
    matches = [entry for entry in caplog.records if getattr(entry, "task_id", None) == "task-1"]
    assert len(matches) == 1
    record = matches[0]
    assert record.raw_priority == "urgent"
    assert record.converted_priority == 2
