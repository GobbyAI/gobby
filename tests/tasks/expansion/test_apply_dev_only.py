"""Dev-only expansion completion must use stage-native completion."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def test_complete_dev_only_run_via_complete_stage() -> None:
    source = source_text("src/gobby/tasks/expansion/_apply.py")

    assert "complete_stage(" in source
    assert "UPDATE tasks SET lifecycle = 'in_development'" not in source
    assert "_skipped_stages" not in source
