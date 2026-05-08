"""Plan archival event handling must key on task_closed events."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def test_archive_keys_on_task_closed_event_not_lifecycle_stage() -> None:
    source = source_text("src/gobby/hooks/event_handlers/_plan.py")

    assert "task_closed" in source
    assert "lifecycle_stage" not in source


def test_terminal_lifecycle_stages_constant_removed() -> None:
    source = source_text("src/gobby/hooks/event_handlers/_plan.py")

    assert "_TERMINAL_LIFECYCLE_STAGES" not in source
