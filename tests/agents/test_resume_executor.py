"""Tests for daemon-stop resume execution."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.agents import resume_executor
from gobby.storage.agents import AgentRun

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_resume_agent_run_merges_persisted_env_with_new_session_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume launch env should keep persisted provider env but use the new child session IDs."""
    original_run = AgentRun(
        id="run-old",
        parent_session_id="parent-old",
        provider="codex",
        prompt="Original prompt",
        status="daemon_stopped",
        created_at="2026-05-30T00:00:00Z",
        updated_at="2026-05-30T00:00:00Z",
        continuation_prompt="Continue",
    )
    resume_metadata: dict[str, Any] = {
        "provider": "codex",
        "provider_native_session_id": "native-123",
        "cwd": "/repo",
        "project_id": "proj-1",
        "parent_session_id": "parent-1",
        "machine_id": "machine-1",
        "env": {
            "PERSISTED": "old",
            "GOBBY_SESSION_ID": "stale-child",
            "GOBBY_AGENT_RUN_ID": "stale-run",
        },
        "sandbox_env": {"SANDBOX": "enabled"},
    }
    persisted_metadata: list[tuple[str, dict[str, Any]]] = []

    def fake_prepare_terminal_spawn(**kwargs: Any) -> SimpleNamespace:
        agent_run_id = kwargs["agent_run_id"]
        return SimpleNamespace(
            session_id="child-new",
            env_vars={
                "GOBBY_SESSION_ID": "child-new",
                "GOBBY_AGENT_RUN_ID": agent_run_id,
            },
        )

    class FakeSpawner:
        env: dict[str, str]

        def spawn(self, *, command: list[str], cwd: str, env: dict[str, str]) -> SimpleNamespace:
            self.env = env
            return SimpleNamespace(
                success=True,
                pid=123,
                tmux_session_name="tmux-new",
                error=None,
                message=None,
            )

    spawner = FakeSpawner()
    run_storage = MagicMock()
    run_storage.update_resume_metadata.side_effect = (
        lambda run_id, metadata: persisted_metadata.append((run_id, metadata))
    )
    runner = SimpleNamespace(
        child_session_manager=MagicMock(),
        run_storage=run_storage,
    )

    monkeypatch.setattr(resume_executor, "prepare_terminal_spawn", fake_prepare_terminal_spawn)
    monkeypatch.setattr(
        resume_executor,
        "build_cli_command",
        lambda **_kwargs: (["codex", "resume"], {}),
    )
    monkeypatch.setattr(resume_executor, "_tmux_spawner", lambda *_args: spawner)
    monkeypatch.setattr(resume_executor, "pre_approve_directory", lambda *_args: None)
    monkeypatch.setattr(resume_executor, "_fire_resume_started", lambda *_args: None)

    result = await resume_executor.resume_agent_run(
        original_run,
        resume_metadata=resume_metadata,
        runner=runner,
        session_manager=MagicMock(),
    )

    assert result.success is True
    assert spawner.env["PERSISTED"] == "old"
    assert spawner.env["SANDBOX"] == "enabled"
    assert spawner.env["GOBBY_SESSION_ID"] == "child-new"
    assert spawner.env["GOBBY_AGENT_RUN_ID"] == result.run_id
    assert spawner.env["GOBBY_MACHINE_ID"] == "machine-1"
    assert len(persisted_metadata) == 1
    assert persisted_metadata[0][0] == result.run_id
    assert persisted_metadata[0][1]["env"] == spawner.env
