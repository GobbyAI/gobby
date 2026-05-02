"""Phase 2 red contracts for stage-aware task listing."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_stage_state_filter": (
            "gobby tasks list filters by exact stage_name and one of the five stage states"
        ),
    },
    required_paths=("src/gobby/cli/tasks/_stage_filters.py",),
)
