from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from gobby.agents.local_model import LocalModelError
from gobby.config.app import DaemonConfig
from gobby.mcp_proxy.tools.spawn_agent._generation_endpoint import resolve_spawn_generation_endpoint

pytestmark = pytest.mark.unit


def _config() -> DaemonConfig:
    return DaemonConfig(
        ai={
            "generation": {
                "endpoints": {
                    "lm-studio": {
                        "protocol": "lmstudio",
                        "api_base": "http://localhost:1234/v1",
                        "model": "qwen-coder-32b",
                        "api_key": "endpoint-token",
                    },
                    "ollama": {
                        "protocol": "ollama",
                        "api_base": "http://localhost:11434",
                        "model": "llama3.2:latest",
                    },
                    "openrouter": {
                        "wire_api": "responses",
                        "api_base": "https://openrouter.ai/api/v1",
                        "api_key": "openrouter-secret",
                        "model": "moonshotai/kimi-k3",
                    },
                }
            }
        }
    )


@pytest.mark.asyncio
async def test_resolve_spawn_generation_endpoint_uses_named_generation_endpoint() -> None:
    run_manager = object()
    with patch(
        "gobby.agents.local_model.ensure_local_model",
        new=AsyncMock(return_value="qwen-coder-32b"),
    ) as ensure_local_model:
        resolution = await resolve_spawn_generation_endpoint(
            model="endpoint:lm-studio",
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "wrapped"),
    [
        (LocalModelError("model unavailable"), True),
        (AttributeError("programming error"), False),
    ],
)
async def test_resolve_spawn_generation_endpoint_only_wraps_local_model_errors(
    error: Exception,
    wrapped: bool,
) -> None:
    with patch(
        "gobby.agents.local_model.ensure_local_model",
        new=AsyncMock(side_effect=error),
    ):
        if wrapped:
            with pytest.raises(ValueError, match="Local model pre-flight failed"):
                await resolve_spawn_generation_endpoint(
                    model="endpoint:lm-studio",
                    api_base=None,
                    api_token=None,
                    daemon_config=_config(),
                    run_manager=None,
                )
        else:
            with pytest.raises(AttributeError, match="programming error"):
                await resolve_spawn_generation_endpoint(
                    model="endpoint:lm-studio",
                    api_base=None,
                    api_token=None,
                    daemon_config=_config(),
                    run_manager=None,
                )


@pytest.mark.asyncio
async def test_resolve_spawn_generation_endpoint_uses_selected_model_override() -> None:
    with patch(
        "gobby.agents.local_model.ensure_local_model",
        new=AsyncMock(return_value="google/gemma-4-26b-a4b-qat"),
    ) as ensure_local_model:
        resolution = await resolve_spawn_generation_endpoint(
            model="endpoint:lm-studio/google/gemma-4-26b-a4b-qat",
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
async def test_resolve_spawn_generation_endpoint_routes_codex_through_oss() -> None:
    with patch(
        "gobby.agents.local_model.ensure_local_model",
        new=AsyncMock(return_value="ollama/qwen3-coder"),
    ) as ensure_local_model:
        resolution = await resolve_spawn_generation_endpoint(
            model="endpoint:ollama/ollama/qwen3-coder",
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
async def test_responses_endpoint_routes_codex_with_child_scoped_overrides() -> None:
    resolution = await resolve_spawn_generation_endpoint(
        model="endpoint:openrouter/moonshotai/kimi-k3",
        api_base=None,
        api_token=None,
        daemon_config=_config(),
        run_manager=None,
        runtime_provider="codex",
    )

    assert resolution.model == "moonshotai/kimi-k3"
    assert resolution.api_base is None
    assert resolution.api_token is None
    assert resolution.is_local is False
    assert 'model_provider="gobby_endpoint_openrouter"' in resolution.codex_config_overrides
    assert resolution.child_env == {"GOBBY_CODEX_ENDPOINT_API_KEY": "openrouter-secret"}
    assert "openrouter-secret" not in repr(resolution.codex_config_overrides)


@pytest.mark.asyncio
async def test_responses_endpoint_rejects_non_codex_runtime() -> None:
    with pytest.raises(ValueError, match="require provider='codex'"):
        await resolve_spawn_generation_endpoint(
            model="endpoint:openrouter/moonshotai/kimi-k3",
            api_base=None,
            api_token=None,
            daemon_config=_config(),
            run_manager=None,
            runtime_provider="claude",
        )


@pytest.mark.asyncio
async def test_resolve_spawn_generation_endpoint_rejects_bare_local_model() -> None:
    with pytest.raises(ValueError, match="model: local has been removed"):
        await resolve_spawn_generation_endpoint(
            model="local",
            api_base=None,
            api_token=None,
            daemon_config=_config(),
            run_manager=None,
        )


@pytest.mark.asyncio
async def test_resolve_spawn_generation_endpoint_preserves_non_local_api_settings() -> None:
    resolution = await resolve_spawn_generation_endpoint(
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
