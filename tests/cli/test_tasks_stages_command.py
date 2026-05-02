"""Phase 2 red contracts for gobby tasks stages."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_command_lives_in_stages_module_not_crud": (
            "the stages command body lives in src/gobby/cli/tasks/stages.py, not crud.py"
        ),
        "test_renders_manifest_with_policy_columns": (
            "gobby tasks stages renders stage position, state, review policy, counters, and caps"
        ),
    },
    required_paths=("src/gobby/cli/tasks/stages.py",),
    required_symbols=("gobby.storage.tasks._stage_states:StageStatesManager",),
)
