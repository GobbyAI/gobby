"""Dev-only expansion completion must use stage-native completion."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def test_complete_dev_only_run_via_complete_stage() -> None:
    apply_source = source_text("src/gobby/tasks/expansion/_apply.py")
    reset_source = source_text("src/gobby/tasks/expansion/_reset.py")

    assert "_complete_parent_expansion_stage_if_current(" in apply_source
    assert "complete_stage(" in reset_source
    assert "UPDATE tasks SET lifecycle = 'in_development'" not in apply_source
    assert "UPDATE tasks SET lifecycle = 'in_development'" not in reset_source
    assert "_skipped_stages" not in apply_source
