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
    },
)
