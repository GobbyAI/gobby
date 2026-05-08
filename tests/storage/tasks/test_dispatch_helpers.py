"""Red tests for task dispatch helper readers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def test_skipped_stages_runtime_helper_removed() -> None:
    from gobby.storage.tasks import _crud

    assert not hasattr(_crud, "_skipped_stages")


def test_is_unattended_reads_unattended_field() -> None:
    from gobby.storage.tasks import _crud

    task = SimpleNamespace(unattended=True, yolo=False)

    assert _crud._is_unattended(task) is True

    legacy_yolo_only = SimpleNamespace(yolo=True)

    assert _crud._is_unattended(legacy_yolo_only) is False
