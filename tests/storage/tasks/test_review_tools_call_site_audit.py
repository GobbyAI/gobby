"""Post-cutover review-tool call-site audit contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.phase5_contract_helpers import source_texts

pytestmark = pytest.mark.unit


def test_all_callers_satisfy_policy() -> None:
    workflows = source_texts(("src/gobby/install/shared/workflows/agents",))

    assert "gobby-tasks:submit_for_review" not in workflows
    assert "gobby-tasks:approve_review" not in workflows
    assert "gobby-tasks:reject_review" not in workflows
    for tool in ("submit_for_review", "approve_review", "reject_review"):
        if f"tool: {tool}" in workflows:
            assert "server: gobby-tasks-ops" in workflows


def test_standalone_test_architect_yaml_removed() -> None:
    assert not Path("src/gobby/install/shared/workflows/agents/test-architect.yaml").exists()


def test_no_residual_success_hook_keyed_on_unused_tool() -> None:
    source = source_texts(("src/gobby/install/shared",))

    assert "on_mcp_success: approve_review" not in source


def test_no_legacy_status_prose_remains_in_skill_or_rule_yaml() -> None:
    source = source_texts(("src/gobby/install/shared/skills", "src/gobby/install/shared/workflows"))

    assert "status becomes needs_review" not in source
    assert "stage stays open" not in source


def test_post_cutover_allowlist_matches_phase_2_6_6() -> None:
    source = source_texts(("src/gobby/install/shared",))

    assert "test-architect.yaml" not in source or "submit_for_review" not in source
