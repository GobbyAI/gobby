"""Phase 2 red contracts for bundled review-tool call-site rewrites."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_call_sites_match_policy_table": (
            "bundled review-tool call sites are policy-aligned with stages.yaml"
        ),
        "test_no_bundled_agent_yaml_or_plan_references_gobby_tasks_prefix_for_other_seven_mutating_stage_tools": (
            "bundled YAML and plans do not route mutating stage tools through gobby-tasks"
        ),
        "test_no_bundled_agent_yaml_references_gobby_tasks_complete_stage": (
            "bundled agent YAML does not reference gobby-tasks:complete_stage"
        ),
        "test_no_disallowed_caller_remains_post_rewrite": (
            "no policy-none stage caller invokes review tools after the rewrite"
        ),
        "test_no_plan_markdown_outside_documented_section_references_gobby_tasks_complete_stage": (
            "plans contain no stray gobby-tasks:complete_stage references outside docs"
        ),
        "test_no_residual_success_hook_keyed_on_unused_tool": (
            "rewritten workflows do not retain success hooks for tools agents no longer call"
        ),
        "test_skill_md_prose_describes_5_state_model": (
            "bundled SKILL.md surfaces describe stage-native submit/approve/reject semantics"
        ),
        "test_test_architect_yaml_all_complete_stage_surfaces_agree_on_gobby_tasks_ops": (
            "test-architect instructions, status, hook, and allowlist agree on gobby-tasks-ops"
        ),
        "test_test_architect_yaml_calls_complete_stage_for_test_arch": (
            "test-architect completes test_arch with complete_stage instead of review tools"
        ),
        "test_test_architect_yaml_complete_stage_success_hook_keyed_on_gobby_tasks_ops": (
            "test-architect success hook is keyed on gobby-tasks-ops:complete_stage"
        ),
        "test_test_architect_yaml_complete_stage_success_hook_sets_handoff_ready": (
            "complete_stage success hook sets handoff_ready=true"
        ),
        "test_test_architect_yaml_complete_stage_uses_gobby_tasks_ops_in_instructions": (
            "test-architect instructions use gobby-tasks-ops:complete_stage"
        ),
        "test_test_architect_yaml_complete_stage_uses_gobby_tasks_ops_in_status_message": (
            "test-architect status message uses gobby-tasks-ops:complete_stage"
        ),
        "test_test_architect_yaml_does_not_call_review_tools": (
            "test-architect YAML does not call mark_task review tools"
        ),
        "test_test_architect_yaml_drops_review_rejected_block_and_prose": (
            "test-architect removes the obsolete mark_task_review_rejected block/prose"
        ),
        "test_test_architect_yaml_workflow_reaches_terminate_after_complete_stage": (
            "test-architect workflow can terminate after the complete_stage success hook"
        ),
    },
    required_text={
        "src/gobby/install/shared/workflows/agents/test-architect.yaml": (
            "gobby-tasks-ops:complete_stage",
            "handoff_ready",
        )
    },
    forbidden_text={
        "src/gobby/install/shared/workflows/agents/test-architect.yaml": (
            "mark_task_review_rejected",
            "mark_task_review_approved",
        )
    },
)
