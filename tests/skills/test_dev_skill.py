"""Red tests for the interactive /gobby dev skill contract."""

from __future__ import annotations

import pytest

from tests.skills.interactive_skill_helpers import (
    assert_interactive_skill_contract,
    read_skill,
)

pytestmark = pytest.mark.unit

SKILL_PATH = "src/gobby/install/shared/skills/dev/SKILL.md"
WORKFLOW_PATH = "src/gobby/install/shared/workflows/dev.yaml"


def test_id_opt_in_present() -> None:
    assert_interactive_skill_contract(
        read_skill(SKILL_PATH),
        name="dev",
        command="/gobby dev",
        agent="developer",
        workflow_path=WORKFLOW_PATH,
    )
