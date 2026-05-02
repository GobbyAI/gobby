"""Post-cutover review-tool call-site audit contracts."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_text, source_texts

pytestmark = pytest.mark.unit


def test_all_callers_satisfy_policy() -> None:
    workflows = source_texts(("src/gobby/install/shared/workflows/agents",))

    assert "gobby-tasks:submit_for_review" not in workflows
    assert "gobby-tasks:approve_review" not in workflows
    assert "gobby-tasks:reject_review" not in workflows
    for tool in ("submit_for_review", "approve_review", "reject_review"):
        if f"tool: {tool}" in workflows:
            assert "server: gobby-tasks-ops" in workflows


def test_test_architect_yaml_does_not_call_review_tools() -> None:
    source = source_text("src/gobby/install/shared/workflows/agents/test-architect.yaml")

    assert "approve_review" not in source
    assert "reject_review" not in source
    assert "submit_for_review" not in source


def test_test_architect_yaml_still_calls_complete_stage_for_test_arch() -> None:
    source = source_text("src/gobby/install/shared/workflows/agents/test-architect.yaml")

    assert "complete_stage" in source
    assert "test_arch" in source


def test_test_architect_yaml_complete_stage_success_hook_still_sets_handoff_ready() -> None:
    source = source_text("src/gobby/install/shared/workflows/agents/test-architect.yaml")

    assert "complete_stage" in source
    assert "handoff_ready" in source


def test_test_architect_yaml_workflow_still_reaches_terminate_after_complete_stage() -> None:
    source = source_text("src/gobby/install/shared/workflows/agents/test-architect.yaml")

    assert "complete_stage" in source
    assert "terminate" in source


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
