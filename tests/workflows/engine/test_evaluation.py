"""Tests for repeated enforcement-denial rendering."""

from __future__ import annotations

import pytest

from gobby.skills.formatting import skill_fetch_batch_directive
from gobby.workflows.engine.evaluation import _repeat_block_reason

pytestmark = pytest.mark.unit


def test_repeat_denial_keeps_step_and_concrete_recovery() -> None:
    directive = skill_fetch_batch_directive(["development-discipline", "restraint", "tasks"])
    reason = (
        "Rule enforced by Gobby: "
        "[step-enforcement:backend-developer/load_required_skills]\n"
        "MCP tool 'gobby-tasks:claim_task' is not allowed.\n"
        f"During this skill-loading step:\n{directive}"
    )

    collapsed = _repeat_block_reason("step-tool-enforcement", reason)

    assert collapsed.startswith(
        "Rule enforced by Gobby: "
        "[step-enforcement:backend-developer/load_required_skills] "
        "(full reason shown earlier this turn — scroll up)."
    )
    for skill in ("development-discipline", "restraint", "tasks"):
        assert f'get_skill", {{"name":"{skill}"}}' in collapsed
    assert "<skill-name>" not in collapsed
