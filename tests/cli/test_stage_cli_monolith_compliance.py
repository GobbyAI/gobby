"""Phase 2 red contracts for stage CLI module ownership."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_all_touched_files_under_1000_lines": (
            "stage CLI work keeps touched CLI source files below the monolith limit or uses "
            "the active refactor backlog"
        ),
        "test_review_module_owns_review_command": (
            "gobby tasks review is implemented in src/gobby/cli/tasks/review.py"
        ),
        "test_stages_module_owns_stages_and_advance_commands": (
            "gobby tasks stages and advance are implemented in src/gobby/cli/tasks/stages.py"
        ),
    },
    required_paths=(
        "src/gobby/cli/tasks/stages.py",
        "src/gobby/cli/tasks/review.py",
    ),
)
