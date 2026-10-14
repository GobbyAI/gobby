"""The durable launch snapshot retains the base selected by isolation."""

import pytest

from gobby.agents.isolation import IsolationContext, SpawnConfig
from gobby.agents.reasoning import SpawnReasoningResolution
from gobby.agents.sandbox import SandboxConfig
from gobby.mcp_proxy.tools.spawn_agent._runtime import build_spawn_context

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("requested", "selected", "expected"),
    [
        (None, "release", "release"),
        ("origin/release", "release", "release"),
        ("new-request", "stored-base", "stored-base"),
        ("explicit", None, "explicit"),
        (None, None, "HEAD"),
    ],
)
async def test_resume_snapshot_retains_effective_base(
    requested: str | None, selected: str | None, expected: str
) -> None:
    config = SpawnConfig(
        prompt="Continue the task",
        task_id=None,
        task_title=None,
        task_seq_num=None,
        branch_name=None,
        branch_prefix=None,
        base_branch=requested,
        project_id="project",
        project_path="/project",
        provider="codex",
        parent_session_id="parent",
    )
    context = IsolationContext(
        cwd="/checkout",
        isolation_type="worktree" if selected else "none",
        extra={"base_branch": selected, "base_commit_sha": "captured-commit"},
    )
    _, snapshot = await build_spawn_context(
        spawn_config=config,
        isolation_ctx=context,
        effective_checkout_mode=context.isolation_type,
        reasoning=SpawnReasoningResolution(None, None, False, "not_requested"),
        initial_variables=None,
        local_context_route=None,
        local_context_observation=None,
        session_manager=None,
        task_additional_skills=None,
        enhanced_prompt=config.prompt,
        requested_model_selector=None,
        effective_sandbox_config=SandboxConfig(),
        effective_workflow=None,
        agent_display_name=None,
        prewarm_pre_commit_store=False,
    )
    assert snapshot["base_branch"] == expected
    assert snapshot["base_commit_sha"] == "captured-commit"
