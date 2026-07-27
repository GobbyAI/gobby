from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from gobby.agents import resume_executor
from gobby.config.app import DaemonConfig
from gobby.storage.agents import AgentRun

_SUCCESSOR_ID = UUID("8d3579d5-f8ac-4db8-8ea6-b29027e8514f")


def _original_run(*, provider: str = "codex") -> AgentRun:
    return AgentRun(
        id="e87bc595-eb81-4cd2-9745-06fc59dcd13d",
        parent_session_id="7d307ae2-5834-43d0-8d59-c385ab37885f",
        child_session_id="0bd17b43-4097-4efe-b16c-4c739ea4787d",
        provider=provider,
        prompt="Original prompt",
        status="cancelled",
        created_at=datetime(2026, 5, 30, tzinfo=UTC),
        updated_at=datetime(2026, 5, 30, tzinfo=UTC),
        continuation_prompt="Continue",
        terminal_reason="daemon_stop",
    )


def _resume_metadata() -> dict[str, Any]:
    return {
        "provider": "codex",
        "provider_native_session_id": "native-123",
        "cwd": "/repo",
        "project_id": "f963cb16-3802-4fcf-b202-0198bb4d271c",
        "parent_session_id": "7d307ae2-5834-43d0-8d59-c385ab37885f",
        "machine_id": "machine-1",
        "env": {
            "UV_CACHE_DIR": "/cache/uv",
            "OPENAI_API_KEY": "must-not-survive",
            "GOBBY_AGENT_RUN_ID": "stale-run",
        },
    }


def _runner(*, storage: MagicMock | None = None) -> SimpleNamespace:
    run_storage = storage or MagicMock()
    run_storage.db = MagicMock()
    running = SimpleNamespace(
        id=str(_SUCCESSOR_ID),
        status="running",
        resume_metadata_json={"daemon_stop_resume_phase": "runtime_persisted"},
    )
    run_storage.transition_resume_phase.return_value = running
    run_storage.start.return_value = running
    run_storage.get.return_value = running
    return SimpleNamespace(
        child_session_manager=MagicMock(),
        run_storage=run_storage,
    )


def _spawn_result(*, success: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        success=success,
        pid=123,
        tmux_session_name="gobby-resume-successor",
        tmux_socket_name="gobby",
        tmux_socket_path="/tmp/gobby.sock",
        error=None if success else "spawn failed",
        message=None,
    )


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    *,
    spawner: MagicMock,
    finalize: MagicMock,
) -> MagicMock:
    prepare = MagicMock(
        return_value=SimpleNamespace(
            session_id=_original_run().child_session_id,
            env_vars={
                "GOBBY_SESSION_ID": _original_run().child_session_id,
                "GOBBY_AGENT_RUN_ID": str(_SUCCESSOR_ID),
            },
        )
    )
    monkeypatch.setattr(uuid, "uuid4", lambda: _SUCCESSOR_ID)
    monkeypatch.setattr(resume_executor, "prepare_terminal_resume", prepare)
    monkeypatch.setattr(resume_executor, "_tmux_spawner", lambda *_args: spawner)
    monkeypatch.setattr(resume_executor, "pre_approve_directory", lambda *_args: None)
    monkeypatch.setattr(resume_executor, "finalize_resume_handoff", finalize)
    monkeypatch.setattr(
        "gobby.agents.resume_finalization.finalize_resume_handoff",
        finalize,
    )
    monkeypatch.setattr(resume_executor, "notify_parent_of_recovery", MagicMock())
    monkeypatch.setattr(resume_executor, "_fire_resume_started", MagicMock())
    return prepare


@pytest.mark.asyncio
async def test_resume_reuses_child_session_and_finalizes_durable_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = MagicMock()
    runner = _runner(storage=storage)
    spawner = MagicMock()
    spawner.spawn.return_value = _spawn_result()
    finalize = MagicMock()
    prepare = _patch_common(monkeypatch, spawner=spawner, finalize=finalize)

    result = await resume_executor.resume_agent_run(
        _original_run(),
        resume_metadata=_resume_metadata(),
        runner=runner,
        session_manager=MagicMock(),
    )

    assert result.success is True
    assert result.run_id == str(_SUCCESSOR_ID)
    assert result.child_session_id == _original_run().child_session_id
    assert prepare.call_args.kwargs["existing_session_id"] == _original_run().child_session_id
    assert prepare.call_args.kwargs["original_run_id"] == _original_run().id
    assert storage.transition_resume_phase.call_args_list[0].kwargs == {
        "expected_phase": "prepared",
        "new_phase": "launch_requested",
    }
    assert storage.transition_resume_phase.call_args_list[1].kwargs == {
        "expected_phase": "launch_requested",
        "new_phase": "runtime_persisted",
    }
    finalize.assert_called_once_with(
        storage.db,
        original_run_id=_original_run().id,
        successor_run_id=str(_SUCCESSOR_ID),
        child_session_id=_original_run().child_session_id,
        completion_registry=None,
    )
    spawn_env = spawner.spawn.call_args.kwargs["env"]
    assert spawn_env["UV_CACHE_DIR"] == "/cache/uv"
    assert "OPENAI_API_KEY" not in spawn_env
    assert spawn_env["GOBBY_AGENT_RUN_ID"] == str(_SUCCESSOR_ID)


@pytest.mark.asyncio
async def test_live_spawn_is_left_provisional_when_runtime_persistence_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = MagicMock()
    storage.update_runtime.side_effect = RuntimeError("database unavailable")
    runner = _runner(storage=storage)
    spawner = MagicMock()
    spawner.spawn.return_value = _spawn_result()
    finalize = MagicMock()
    _patch_common(monkeypatch, spawner=spawner, finalize=finalize)

    result = await resume_executor.resume_agent_run(
        _original_run(),
        resume_metadata=_resume_metadata(),
        runner=runner,
        session_manager=MagicMock(),
    )

    assert result.success is True
    assert result.error == "resume_runtime_persist_failed:RuntimeError"
    finalize.assert_not_called()
    storage.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_spawn_failure_parks_successor_without_releasing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = MagicMock()
    runner = _runner(storage=storage)
    spawner = MagicMock()
    spawner.spawn.return_value = _spawn_result(success=False)
    finalize = MagicMock()
    cleanup_runtime = MagicMock()
    _patch_common(monkeypatch, spawner=spawner, finalize=finalize)
    monkeypatch.setattr(
        "gobby.agents.runtime_cleanup.cleanup_agent_runtime_state",
        cleanup_runtime,
    )

    result = await resume_executor.resume_agent_run(
        _original_run(),
        resume_metadata=_resume_metadata(),
        runner=runner,
        session_manager=MagicMock(),
    )

    assert result.success is False
    assert result.error is not None
    finalize.assert_called_once()
    assert finalize.call_count == 1
    storage.cancel.assert_called_once_with(
        str(_SUCCESSOR_ID),
        terminal_reason="daemon_stop",
    )
    assert storage.cancel.call_count == 1
    cleanup_runtime.assert_called_once_with(
        storage.db,
        run_id=str(_SUCCESSOR_ID),
        child_session_id=_original_run().child_session_id,
        terminal_reason="daemon_stop",
    )
    assert cleanup_runtime.call_count == 1


@pytest.mark.asyncio
async def test_resume_rejects_relative_cwd_before_preparing_successor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare = MagicMock()
    monkeypatch.setattr(resume_executor, "prepare_terminal_resume", prepare)
    metadata = _resume_metadata()
    metadata["cwd"] = "relative/repo"

    result = await resume_executor.resume_agent_run(
        _original_run(),
        resume_metadata=metadata,
        runner=_runner(),
        session_manager=MagicMock(),
    )

    assert result.success is False
    assert result.error == "resume_cwd_not_absolute"
    prepare.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resume_responses_endpoint_rebuilds_child_scoped_codex_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_override = 'model_provider="gobby_endpoint_openrouter"'
    metadata = _resume_metadata()
    metadata["model"] = "endpoint:openrouter/moonshotai/kimi-k3"
    metadata["config_overrides"] = [provider_override]
    storage = MagicMock()
    runner = _runner(storage=storage)
    spawner = MagicMock()
    spawner.spawn.return_value = _spawn_result()
    finalize = MagicMock()
    prepare = _patch_common(monkeypatch, spawner=spawner, finalize=finalize)
    build_cli = MagicMock(return_value=(["codex", "resume"], {}))
    monkeypatch.setattr(resume_executor, "build_cli_command", build_cli)

    result = await resume_executor.resume_agent_run(
        _original_run(),
        resume_metadata=metadata,
        runner=runner,
        session_manager=MagicMock(),
        daemon_config=DaemonConfig(
            ai={
                "generation": {
                    "endpoints": {
                        "openrouter": {
                            "protocol": "openai-compatible",
                            "wire_api": "responses",
                            "api_base": "https://openrouter.ai/api/v1",
                            "api_key": "sk-openrouter-test",
                            "model": "moonshotai/kimi-k3",
                        }
                    }
                }
            }
        ),
    )

    assert result.success is True
    build_kwargs = build_cli.call_args.kwargs
    assert build_kwargs["cli"] == "codex"
    assert build_kwargs["model"] == "moonshotai/kimi-k3"
    assert build_kwargs["resume_session_id"] == "native-123"
    overrides = build_kwargs["config_overrides"]
    assert overrides.count(provider_override) == 1
    assert 'model="moonshotai/kimi-k3"' in overrides
    assert (
        'model_providers.gobby_endpoint_openrouter.base_url="https://openrouter.ai/api/v1"'
        in overrides
    )
    spawn_env = spawner.spawn.call_args.kwargs["env"]
    assert spawn_env["GOBBY_CODEX_ENDPOINT_API_KEY"] == "sk-openrouter-test"
    assert "sk-openrouter-test" not in repr(build_kwargs)
    assert "sk-openrouter-test" not in repr(prepare.call_args.kwargs)
    merge_calls = storage.merge_resume_metadata.call_args_list
    assert merge_calls
    assert all("sk-openrouter-test" not in repr(call) for call in merge_calls)
    launch_updates = merge_calls[0].args[1]
    assert launch_updates["config_overrides"].count(provider_override) == 1
    assert 'model="moonshotai/kimi-k3"' in launch_updates["config_overrides"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "base_env", "token_env"),
    [
        ("droid", "FACTORY_API_BASE_URL", "FACTORY_API_KEY"),
        ("grok", "GROK_API_BASE", "XAI_API_KEY"),
        ("qwen", "QWEN_API_BASE", "QWEN_API_KEY"),
    ],
)
async def test_resume_non_codex_endpoint_uses_provider_specific_environment(
    provider: str,
    base_env: str,
    token_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _resume_metadata()
    metadata["provider"] = provider
    metadata["model"] = "endpoint:resume-test/provider-model"
    storage = MagicMock()
    runner = _runner(storage=storage)
    spawner = MagicMock()
    spawner.spawn.return_value = _spawn_result()
    _patch_common(monkeypatch, spawner=spawner, finalize=MagicMock())
    monkeypatch.setattr(
        "gobby.agents.resume_executor.shutil.which",
        lambda _command: "/usr/bin/provider",
    )
    monkeypatch.setattr(
        resume_executor,
        "build_cli_command",
        MagicMock(return_value=([provider, "resume"], {})),
    )

    result = await resume_executor.resume_agent_run(
        _original_run(provider=provider),
        resume_metadata=metadata,
        runner=runner,
        session_manager=MagicMock(),
        daemon_config=DaemonConfig(
            ai={
                "generation": {
                    "endpoints": {
                        "resume-test": {
                            "protocol": "openai-compatible",
                            "wire_api": "chat-completions",
                            "api_base": "https://resume.example/v1",
                            "api_key": "resume-secret",
                            "model": "provider-model",
                        }
                    }
                }
            }
        ),
    )

    assert result.success is True
    spawn_env = spawner.spawn.call_args.kwargs["env"]
    assert spawn_env[base_env] == "https://resume.example/v1"
    assert spawn_env[token_env] == "resume-secret"
