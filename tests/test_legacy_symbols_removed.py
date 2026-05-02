"""Legacy lifecycle/status symbols must be gone after Phase 5 cutover."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_texts

pytestmark = pytest.mark.unit


def test_no_lifecycle_imports() -> None:
    scoped = source_texts(("src/gobby",))

    assert "Lifecycle" not in scoped
    assert "TaskLifecycleStage" not in scoped
    assert "project_legacy_status" not in scoped
    assert "lifecycle_stage_from_status" not in scoped
