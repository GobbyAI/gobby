from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from gobby.agents import resume_executor
from gobby.agents.srt_runtime import SandboxLaunch
from gobby.ai.codex_endpoint import CODEX_ENDPOINT_API_KEY_ENV
from gobby.config.app import DaemonConfig
from gobby.storage.agents import AgentRun
from tests.terminals.fakes import bind_spawn_runtime

pytestmark = pytest.mark.unit

_SUCCESSOR_ID = UUID("8d3579d5-f8ac-4db8-8ea6-b29027e8514f")


@pytest.fixture(autouse=True)
def mock_codex_prompt_delivery(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Keep the fire-and-forget Codex prompt delivery task out of resume tests.

    Codex resumes schedule a real background coroutine against the spawner's
    tmux session manager; against MagicMock spawners that coroutine would
    outlive the test's event loop. Tests that assert delivery use this mock.
    """
    mock_delivery = MagicMock(return_value=True)
    monkeypatch.setattr(resume_executor, "schedule_codex_prompt_delivery", mock_delivery)
    return mock_delivery


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
        "machine_id": "21000000-0000-4000-8000-000000000001",
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
    bound = SimpleNamespace()
    manager, runtime = bind_spawn_runtime(bound)
    return SimpleNamespace(
        child_session_manager=MagicMock(),
        run_storage=run_storage,
        terminal_manager=manager,
        write_coordinator=bound.write_coordinator,
        terminal_runtime_registry=SimpleNamespace(
            resolve=lambda _backend: runtime,
        ),
        _test_runtime=runtime,
    )


def _spawn_result(*, success: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        success=success,
        pid=123,
        terminal_id="gobby-resume-successor",
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
    monkeypatch.setattr(resume_executor, "pre_approve_directory", lambda *_args: None)
    monkeypatch.setattr(resume_executor, "finalize_resume_handoff_async", finalize)
    monkeypatch.setattr(
        "gobby.agents.resume_finalization.finalize_resume_handoff_async",
        finalize,
    )
    monkeypatch.setattr(resume_executor, "notify_parent_of_recovery", MagicMock())
    monkeypatch.setattr(resume_executor, "_fire_resume_started", MagicMock())
    return prepare


@pytest.mark.asyncio
async def test_codex_resume_delivers_prompt_via_composer_not_argv(
    monkeypatch: pytest.MonkeyPatch,
    mock_codex_prompt_delivery: MagicMock,
) -> None:
    """A CLI-argument prompt cancels Codex's in-flight MCP client startup."""
    runner = _runner()
    spawner = MagicMock()
    spawner.spawn.return_value = _spawn_result()
    finalize = AsyncMock()
    _patch_common(monkeypatch, spawner=spawner, finalize=finalize)

    result = await resume_executor.resume_agent_run(
        _original_run(),
        resume_metadata=_resume_metadata(),
        runner=runner,
        session_manager=MagicMock(),
    )

    assert result.success is True
    command = runner._test_runtime.last_request.command
    assert command[0:2] == ["codex", "resume"]
    assert command[-1] == "native-123"
    assert "Continue" not in command
    mock_codex_prompt_delivery.assert_called_once()
    delivery_args = mock_codex_prompt_delivery.call_args.args
    assert delivery_args[0] is runner.write_coordinator
    assert delivery_args[1].spawn_key == runner._test_runtime.last_request.spawn_key
    assert delivery_args[2] == "Continue"
    assert delivery_args[3] == str(_SUCCESSOR_ID)
    assert delivery_args[4] is runner.run_storage


@pytest.mark.asyncio
async def test_claude_resume_keeps_prompt_in_argv(
    monkeypatch: pytest.MonkeyPatch,
    mock_codex_prompt_delivery: MagicMock,
) -> None:
    metadata = _resume_metadata()
    metadata["provider"] = "claude"
    runner = _runner()
    spawner = MagicMock()
    spawner.spawn.return_value = _spawn_result()
    finalize = AsyncMock()
    _patch_common(monkeypatch, spawner=spawner, finalize=finalize)

    result = await resume_executor.resume_agent_run(
        _original_run(provider="claude"),
        resume_metadata=metadata,
        runner=runner,
        session_manager=MagicMock(),
    )

    assert result.success is True
    command = runner._test_runtime.last_request.command
    assert command[-1] == "Continue"
    mock_codex_prompt_delivery.assert_not_called()


@pytest.mark.asyncio
async def test_resume_reuses_child_session_and_finalizes_durable_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = MagicMock()
    runner = _runner(storage=storage)
    spawner = MagicMock()
    spawner.spawn.return_value = _spawn_result()
    finalize = AsyncMock()
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
    spawn_env = runner._test_runtime.last_request.env
    assert spawn_env["UV_CACHE_DIR"] == "/cache/uv"
    assert "OPENAI_API_KEY" not in spawn_env
    assert spawn_env["GOBBY_AGENT_RUN_ID"] == str(_SUCCESSOR_ID)


@pytest.mark.asyncio
async def test_srt_resume_executes_resolved_provider_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = MagicMock()
    runner = _runner(storage=storage)
    spawner = MagicMock()
    spawner.spawn.return_value = _spawn_result()
    finalize = AsyncMock()
    _patch_common(monkeypatch, spawner=spawner, finalize=finalize)
    target = "/opt/claude/versions/2.1.220"
    launch = SandboxLaunch(
        backend="srt",
        enforced=True,
        provider_executable=target,
        policy_path="/policy/settings.json",
        violation_path="/policy/violations.jsonl",
        node_path="/managed/node",
        runner_path="/managed/runner.mjs",
    )
    prepare_sandbox = AsyncMock(return_value=launch)
    monkeypatch.setattr(resume_executor, "prepare_sandbox_launch", prepare_sandbox)
    metadata = _resume_metadata()
    metadata.update(
        {
            "provider": "claude",
            "sandbox_config": {
                "enabled": True,
                "backend": "srt",
                "allow_network": False,
            },
        }
    )

    result = await resume_executor.resume_agent_run(
        _original_run(provider="claude"),
        resume_metadata=metadata,
        runner=runner,
        session_manager=MagicMock(),
    )

    assert result.success is True
    spawn_kwargs = {
        "command": runner._test_runtime.last_request.command,
        "cwd": runner._test_runtime.last_request.cwd,
        "env": runner._test_runtime.last_request.env,
        "auth_cli": runner._test_runtime.last_request.auth_cli,
    }
    command = spawn_kwargs["command"]
    assert command[command.index("--") + 1] == target
    assert spawn_kwargs["auth_cli"] == "claude"
    prepare_sandbox.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_spawn_is_left_provisional_when_runtime_persistence_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = MagicMock()
    storage.update_runtime.side_effect = RuntimeError("database unavailable")
    runner = _runner(storage=storage)
    spawner = MagicMock()
    spawner.spawn.return_value = _spawn_result()
    finalize = AsyncMock()
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
    finalize = AsyncMock()
    cleanup_runtime = MagicMock()
    _patch_common(monkeypatch, spawner=spawner, finalize=finalize)
    runner._test_runtime.typed_fail = True
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
    finalize = AsyncMock()
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
    spawn_env = runner._test_runtime.last_request.env
    assert spawn_env["GOBBY_CODEX_ENDPOINT_API_KEY"] == "sk-openrouter-test"
    assert "sk-openrouter-test" not in repr(build_kwargs)
    assert "sk-openrouter-test" not in repr(prepare.call_args.kwargs)
    merge_calls = storage.merge_resume_metadata.call_args_list
    assert merge_calls, "launch updates must be merged onto the successor"
    assert all("sk-openrouter-test" not in repr(call) for call in merge_calls)
    launch_updates = merge_calls[0].args[1]
    assert launch_updates["config_overrides"].count(provider_override) == 1
    assert 'model="moonshotai/kimi-k3"' in launch_updates["config_overrides"]


@pytest.mark.asyncio
async def test_resume_vllm_endpoint_uses_config_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-vllm-resume-never-in-argv"
    metadata = _resume_metadata()
    metadata["model"] = "endpoint:metal/Qwen/Qwen2.5-7B-Instruct"
    storage = MagicMock()
    runner = _runner(storage=storage)
    spawner = MagicMock()
    spawner.spawn.return_value = _spawn_result()
    finalize = AsyncMock()
    _patch_common(monkeypatch, spawner=spawner, finalize=finalize)
    build_cli = MagicMock(return_value=(["codex", "resume"], {}))
    monkeypatch.setattr(resume_executor, "build_cli_command", build_cli)
    ensure_local_model = AsyncMock(return_value="Qwen/Qwen2.5-7B-Instruct")
    monkeypatch.setattr(resume_executor, "ensure_local_model", ensure_local_model)

    result = await resume_executor.resume_agent_run(
        _original_run(),
        resume_metadata=metadata,
        runner=runner,
        session_manager=MagicMock(),
        daemon_config=DaemonConfig(
            ai={
                "generation": {
                    "endpoints": {
                        "metal": {
                            "protocol": "vllm",
                            "api_base": "http://127.0.0.1:8000/v1",
                            "api_key": secret,
                            "model": "Qwen/Qwen2.5-7B-Instruct",
                        }
                    }
                }
            }
        ),
    )

    assert result.success is True
    ensure_local_model.assert_awaited_once()
    build_kwargs = build_cli.call_args.kwargs
    assert build_kwargs["cli"] == "codex"
    assert build_kwargs["codex_oss_provider"] is None
    assert build_kwargs["model"] == "Qwen/Qwen2.5-7B-Instruct"
    overrides = build_kwargs["config_overrides"]
    assert 'model_provider="gobby-vllm-metal"' in overrides
    assert 'model_providers.gobby-vllm-metal.wire_api="responses"' in overrides
    assert f'model_providers.gobby-vllm-metal.env_key="{CODEX_ENDPOINT_API_KEY_ENV}"' in overrides
    assert f'shell_environment_policy.exclude=["{CODEX_ENDPOINT_API_KEY_ENV}"]' in overrides
    assert "--oss" not in repr(build_kwargs)
    assert secret not in repr(build_kwargs)
    spawned = runner._test_runtime.last_request
    assert spawned is not None and spawned.env is not None
    assert spawned.env[CODEX_ENDPOINT_API_KEY_ENV] == secret


@pytest.mark.asyncio
async def test_resume_never_replays_stored_secret_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-allowlisted stored overrides are dropped; the capability is re-minted."""
    legacy_token_override = 'mcp_servers.gobby.env.GOBBY_AGENT_API_TOKEN="stale-capability"'
    metadata = _resume_metadata()
    metadata["config_overrides"] = [
        legacy_token_override,
        'mcp_servers.gobby.command="uv"',
    ]
    runner = _runner()
    spawner = MagicMock()
    spawner.spawn.return_value = _spawn_result()
    finalize = AsyncMock()
    _patch_common(monkeypatch, spawner=spawner, finalize=finalize)
    build_cli = MagicMock(return_value=(["codex", "resume"], {}))
    monkeypatch.setattr(resume_executor, "build_cli_command", build_cli)

    result = await resume_executor.resume_agent_run(
        _original_run(),
        resume_metadata=metadata,
        runner=runner,
        session_manager=MagicMock(),
    )

    assert result.success is True
    overrides = build_cli.call_args.kwargs["config_overrides"]
    assert 'mcp_servers.gobby.command="uv"' in overrides
    assert legacy_token_override not in overrides
    assert "stale-capability" not in repr(build_cli.call_args)


@pytest.mark.parametrize(
    ("provider", "base_env", "token_env"),
    [
        ("droid", "FACTORY_API_BASE_URL", "FACTORY_API_KEY"),
        ("grok", "GROK_API_BASE", "XAI_API_KEY"),
        ("qwen", "QWEN_API_BASE", "QWEN_API_KEY"),
    ],
)
@pytest.mark.asyncio
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
    _patch_common(monkeypatch, spawner=spawner, finalize=AsyncMock())
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
    spawn_env = runner._test_runtime.last_request.env
    assert spawn_env[base_env] == "https://resume.example/v1"
    assert spawn_env[token_env] == "resume-secret"


@pytest.mark.asyncio
async def test_resume_reuses_persisted_claude_mcp_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _resume_metadata()
    metadata["provider"] = "claude"
    metadata["mcp_path"] = "/persisted/.mcp.json"
    metadata["strict_mcp"] = True
    storage = MagicMock()
    runner = _runner(storage=storage)
    spawner = MagicMock()
    spawner.spawn.return_value = _spawn_result()
    finalize = AsyncMock()
    prepare = _patch_common(monkeypatch, spawner=spawner, finalize=finalize)

    result = await resume_executor.resume_agent_run(
        _original_run(provider="claude"),
        resume_metadata=metadata,
        runner=runner,
        session_manager=MagicMock(),
    )

    assert result.success is True
    command = runner._test_runtime.last_request.command
    assert command[0:3] == ["claude", "--resume", "native-123"]
    assert command[command.index("--mcp-config") + 1] == "/persisted/.mcp.json"
    assert command.index("--strict-mcp-config") == command.index("--mcp-config") + 2
    assert command.index("--strict-mcp-config") < command.index("Continue")
    assert command[-1] == "Continue"
    successor_metadata = prepare.call_args.kwargs["resume_metadata_json"]
    assert successor_metadata["mcp_path"] == "/persisted/.mcp.json"
    assert successor_metadata["strict_mcp"] is True
    launch_updates = storage.merge_resume_metadata.call_args_list[0].args[1]
    assert "mcp_path" not in launch_updates
    assert "strict_mcp" not in launch_updates


@pytest.mark.asyncio
async def test_resume_discovers_workspace_mcp_config_for_claude(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mcp_config = tmp_path / ".mcp.json"
    mcp_config.write_text(
        '{"mcpServers":{"gobby":{"command":"uv","args":["run","gobby","mcp-server"]}}}',
        encoding="utf-8",
    )
    metadata = _resume_metadata()
    metadata["provider"] = "claude"
    metadata["cwd"] = str(tmp_path)
    storage = MagicMock()
    runner = _runner(storage=storage)
    spawner = MagicMock()
    spawner.spawn.return_value = _spawn_result()
    finalize = AsyncMock()
    _patch_common(monkeypatch, spawner=spawner, finalize=finalize)

    result = await resume_executor.resume_agent_run(
        _original_run(provider="claude"),
        resume_metadata=metadata,
        runner=runner,
        session_manager=MagicMock(),
    )

    assert result.success is True
    command = runner._test_runtime.last_request.command
    assert command[0:3] == ["claude", "--resume", "native-123"]
    assert command[command.index("--mcp-config") + 1] == str(mcp_config)
    assert command.index("--strict-mcp-config") < command.index("Continue")
    assert command[-1] == "Continue"
    launch_updates = storage.merge_resume_metadata.call_args_list[0].args[1]
    assert launch_updates["mcp_path"] == str(mcp_config)
    assert launch_updates["strict_mcp"] is True
    ordered = [name for name, _args, _kwargs in storage.mock_calls]
    assert ordered.index("merge_resume_metadata") < ordered.index("transition_resume_phase")


@pytest.mark.asyncio
async def test_successor_metadata_strips_inherited_protocol_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _resume_metadata()
    metadata.update(
        {
            "daemon_stop_resume_phase": "finalized",
            "daemon_stop_resume_consumed_at": "2026-05-30T00:00:00+00:00",
            "daemon_stop_resume_consumed_by_run_id": "stale-successor",
            "daemon_stop_resume_failure_count": 2,
            "daemon_stop_resume_finalized_at": "2026-05-30T00:00:00+00:00",
            "daemon_stop_resume_terminal_id": "stale-tmux",
            "daemon_stop_resume_spawn_key": "stale-key",
            "daemon_stop_orphan_reap_started_at": "2026-05-30T00:00:00+00:00",
            "daemon_stop_orphan_reap_requested_at": "2026-05-30T00:00:00+00:00",
            "daemon_stop_orphan_reaped_at": "2026-05-30T00:00:00+00:00",
            "reconciliation_pending": True,
            "resumed_from_run_id": "stale",
        }
    )
    storage = MagicMock()
    runner = _runner(storage=storage)
    spawner = MagicMock()
    spawner.spawn.return_value = _spawn_result()
    finalize = AsyncMock()
    prepare = _patch_common(monkeypatch, spawner=spawner, finalize=finalize)

    result = await resume_executor.resume_agent_run(
        _original_run(),
        resume_metadata=metadata,
        runner=runner,
        session_manager=MagicMock(),
    )

    assert result.success is True
    successor_metadata = prepare.call_args.kwargs["resume_metadata_json"]
    refreshed_keys = {
        "daemon_stop_resume_phase",
        "resumed_from_run_id",
    }
    for key in resume_executor._INHERITED_PROTOCOL_KEYS:
        assert key in metadata, f"test must seed protocol key {key!r}"
        if key in refreshed_keys:
            continue
        assert key not in successor_metadata, f"protocol key {key!r} leaked into successor"
    assert successor_metadata["resumed_from_run_id"] == _original_run().id
    assert successor_metadata["daemon_stop_resume_phase"] == "prepared"
    assert "daemon_stop_resume_planned_tmux_title" not in successor_metadata
    assert "daemon_stop_resume_spawn_key" not in successor_metadata


@pytest.mark.asyncio
async def test_daemon_stop_resume_uses_terminal_id_and_spawn_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.storage import daemon_resume_keys

    storage = MagicMock()
    runner = _runner(storage=storage)
    spawner = MagicMock()
    finalize = AsyncMock()
    _patch_common(monkeypatch, spawner=spawner, finalize=finalize)

    result = await resume_executor.resume_agent_run(
        _original_run(),
        resume_metadata=_resume_metadata(),
        runner=runner,
        session_manager=MagicMock(),
    )

    assert result.success is True
    identity_writes = [
        call.args[1]
        for call in storage.merge_resume_metadata.call_args_list
        if daemon_resume_keys.TERMINAL_ID_KEY in call.args[1]
        or daemon_resume_keys.SPAWN_KEY_KEY in call.args[1]
    ]
    assert identity_writes
    written = identity_writes[-1]
    assert daemon_resume_keys.TERMINAL_ID_KEY in written
    assert daemon_resume_keys.SPAWN_KEY_KEY in written
    assert all("session_name" not in key for key in written)
    assert all("planned" not in key for key in written)
    runtime_kwargs = storage.update_runtime.call_args.kwargs
    assert runtime_kwargs["terminal_id"] == written[daemon_resume_keys.TERMINAL_ID_KEY]


@pytest.mark.asyncio
async def test_resume_vllm_endpoint_reports_unresolved_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolved $secret: api_key fails the resume contract instead of raising."""
    metadata = _resume_metadata()
    metadata["model"] = "endpoint:metal/Qwen/Qwen2.5-7B-Instruct"
    runner = _runner(storage=MagicMock())
    spawner = MagicMock()
    spawner.spawn.return_value = _spawn_result()
    _patch_common(monkeypatch, spawner=spawner, finalize=AsyncMock())
    monkeypatch.setattr(resume_executor, "build_cli_command", MagicMock())
    monkeypatch.setattr(
        resume_executor,
        "ensure_local_model",
        AsyncMock(return_value="Qwen/Qwen2.5-7B-Instruct"),
    )

    result = await resume_executor.resume_agent_run(
        _original_run(),
        resume_metadata=metadata,
        runner=runner,
        session_manager=MagicMock(),
        daemon_config=DaemonConfig(
            ai={
                "generation": {
                    "endpoints": {
                        "metal": {
                            "protocol": "vllm",
                            "api_base": "http://127.0.0.1:8000/v1",
                            "api_key": "$secret:missing",
                            "model": "Qwen/Qwen2.5-7B-Instruct",
                        }
                    }
                }
            }
        ),
    )

    assert result.success is False
    assert "secret" in (result.error or "")
    assert runner._test_runtime.create_calls == 0
