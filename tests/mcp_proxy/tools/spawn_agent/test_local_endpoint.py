from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.mcp_proxy.tools.spawn_agent._local_endpoint import resolve_spawn_local_endpoint

pytestmark = pytest.mark.unit


def _config() -> DaemonConfig:
    return DaemonConfig(
        ai={
            "generation": {
                "local": {
                    "endpoints": {
                        "lm-studio": {
                            "provider": "lmstudio",
                            "api_base": "http://localhost:1234/v1",
                            "model": "qwen-coder-32b",
                            "api_key": "endpoint-token",
                        },
                        "ollama": {
                            "provider": "ollama",
                            "api_base": "http://localhost:11434",
                            "model": "llama3.2:latest",
                        },
                    }
                }
            }
        }
    )


@pytest.mark.asyncio
async def test_resolve_spawn_local_endpoint_uses_named_generation_endpoint() -> None:
    run_manager = object()
    with patch(
        "gobby.agents.local_model.ensure_local_model",
        new=AsyncMock(return_value="qwen-coder-32b"),
    ) as ensure_local_model:
        resolution = await resolve_spawn_local_endpoint(
            model="local:lm-studio",
            api_base=None,
            api_token=None,
            daemon_config=_config(),
            run_manager=run_manager,
        )

    assert resolution.model == "qwen-coder-32b"
    assert resolution.api_base == "http://localhost:1234/v1"
    assert resolution.api_token == "endpoint-token"
    assert resolution.is_local is True
    ensure_local_model.assert_awaited_once()
    assert ensure_local_model.await_args.kwargs == {"run_manager": run_manager}
    assert ensure_local_model.await_args.args[0].model == "qwen-coder-32b"


@pytest.mark.asyncio
async def test_resolve_spawn_local_endpoint_uses_selected_model_override() -> None:
    with patch(
        "gobby.agents.local_model.ensure_local_model",
        new=AsyncMock(return_value="google/gemma-4-26b-a4b-qat"),
    ) as ensure_local_model:
        resolution = await resolve_spawn_local_endpoint(
            model="local:lm-studio/google/gemma-4-26b-a4b-qat",
            api_base=None,
            api_token=None,
            daemon_config=_config(),
            run_manager=None,
        )

    assert resolution.model == "google/gemma-4-26b-a4b-qat"
    assert resolution.api_base == "http://localhost:1234/v1"
    assert resolution.api_token == "endpoint-token"
    assert resolution.is_local is True
    assert ensure_local_model.await_args.args[0].model == "google/gemma-4-26b-a4b-qat"


@pytest.mark.asyncio
async def test_resolve_spawn_local_endpoint_routes_codex_through_oss() -> None:
    with patch(
        "gobby.agents.local_model.ensure_local_model",
        new=AsyncMock(return_value="ollama/qwen3-coder"),
    ) as ensure_local_model:
        resolution = await resolve_spawn_local_endpoint(
            model="local:ollama/ollama/qwen3-coder",
            api_base=None,
            api_token=None,
            daemon_config=_config(),
            run_manager=None,
            runtime_provider="codex",
        )

    assert resolution.model == "ollama/qwen3-coder"
    assert resolution.api_base is None
    assert resolution.api_token is None
    assert resolution.is_local is True
    assert resolution.codex_oss_provider == "ollama"
    assert ensure_local_model.await_args.args[0].model == "ollama/qwen3-coder"


@pytest.mark.asyncio
async def test_resolve_spawn_local_endpoint_rejects_bare_local_model() -> None:
    with pytest.raises(ValueError, match="model: local has been removed"):
        await resolve_spawn_local_endpoint(
            model="local",
            api_base=None,
            api_token=None,
            daemon_config=_config(),
            run_manager=None,
        )


@pytest.mark.asyncio
async def test_resolve_spawn_local_endpoint_preserves_non_local_api_settings() -> None:
    resolution = await resolve_spawn_local_endpoint(
        model="sonnet",
        api_base="http://custom.example/v1",
        api_token="agent-token",
        daemon_config=_config(),
        run_manager=None,
    )

    assert resolution.model == "sonnet"
    assert resolution.api_base == "http://custom.example/v1"
    assert resolution.api_token == "agent-token"
    assert resolution.is_local is False
