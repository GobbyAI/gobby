"""Red tests for task dispatch helper readers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def test_skipped_stages_parses_labels() -> None:
    from gobby.storage.tasks import _crud

    assert _crud._skipped_stages(
        [
            "priority:high",
            "stage-:plan_review",
            "stage-:qa",
            "stage-:qa",
            "stage-:",
            "stage-:  ",
            "stage-plan",
            None,
        ]
    ) == {"plan_review", "qa"}


def test_is_unattended_reads_unattended_field() -> None:
    from gobby.storage.tasks import _crud

    task = SimpleNamespace(unattended=True, yolo=False)

    assert _crud._is_unattended(task) is True

    legacy_yolo_only = SimpleNamespace(yolo=True)

    assert _crud._is_unattended(legacy_yolo_only) is False
