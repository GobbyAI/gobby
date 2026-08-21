"""Tests for local generation configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from gobby.agents.codex_oss import codex_oss_launch_args
from gobby.ai.codex_endpoint import (
    CODEX_ENDPOINT_API_KEY_ENV,
    codex_endpoint_app_server_env,
    codex_endpoint_config_overrides,
)
from gobby.config.ai import (
    GenerationConfig,
    GenerationEndpointConfig,
    GenerationEndpointProtocol,
)
from gobby.config.app import DaemonConfig
from gobby.servers.local_provider_models import LocalEndpointModelGroup
from gobby.servers.routes.providers import _local_generation_provider_entries
from gobby.servers.websocket.chat.backends import CodexManagedChatSession
from gobby.servers.websocket.chat.runtime_manager import WebChatRuntimeManager

pytestmark = pytest.mark.unit


class TestGenerationEndpointConfig:
    """Tests for ai.generation.endpoints config."""

    def test_defaults(self) -> None:
        cfg = GenerationConfig()

        assert cfg.endpoints == {}

    def test_endpoint_with_api_base_and_model(self) -> None:
        cfg = GenerationConfig(
            endpoints={
                "lm-studio": GenerationEndpointConfig(
                    api_base="http://localhost:1234/v1",
                    model="qwen-coder",
                    api_key="local-key",
                ),
                "ollama": {
                    "api_base": "http://localhost:11434/v1",
                    "model": "qwen2.5-coder",
                },
            }
        )

        assert cfg.endpoints["lm-studio"].api_base == "http://localhost:1234/v1"
        assert cfg.endpoints["lm-studio"].protocol == "openai-compatible"
        assert cfg.endpoints["lm-studio"].model == "qwen-coder"
        assert cfg.endpoints["lm-studio"].api_key == "local-key"
        assert cfg.endpoints["lm-studio"].input_modalities is None
        assert cfg.endpoints["ollama"].model == "qwen2.5-coder"

    @pytest.mark.parametrize("protocol", ["openai-compatible", "lmstudio", "ollama"])
    def test_endpoint_accepts_supported_protocols(self, protocol: str) -> None:
        endpoint = GenerationEndpointConfig(
            protocol=protocol,
            api_base="http://localhost:1234",
            model="qwen-coder",
        )

        assert endpoint.protocol == protocol

    def test_endpoint_rejects_unknown_protocol(self) -> None:
        with pytest.raises(ValidationError, match="protocol"):
            GenerationEndpointConfig(
                protocol="lm-studio",
                api_base="http://localhost:1234",
                model="qwen-coder",
            )

    def test_endpoint_requires_api_base(self) -> None:
        with pytest.raises(ValidationError, match="api_base"):
            GenerationEndpointConfig(api_base="", model="qwen-coder")

    def test_endpoint_requires_model(self) -> None:
        with pytest.raises(ValidationError, match="model"):
            GenerationEndpointConfig(api_base="http://localhost:1234/v1", model="")

    @pytest.mark.parametrize("name", ["", "lm/studio", "lm:studio", "LmStudio"])
    def test_endpoint_names_must_be_lowercase_slugs(self, name: str) -> None:
        with pytest.raises(ValidationError, match="endpoint names"):
            GenerationConfig(
                endpoints={
                    name: {
                        "api_base": "http://localhost:1234/v1",
                        "model": "qwen-coder",
                    }
                }
            )

    def test_daemon_config_has_ai_generation_endpoints(self) -> None:
        config = DaemonConfig()

        assert config.ai.generation.endpoints == {}

    def test_daemon_config_rejects_removed_generation_local(self) -> None:
        with pytest.raises(ValidationError, match=r"ai\.generation\.local"):
            DaemonConfig(ai={"generation": {"local": {"endpoints": {}}}})

    def test_daemon_config_rejects_top_level_local(self) -> None:
        with pytest.raises(ValidationError, match="local config has been removed"):
            DaemonConfig(local={"url": "http://localhost:1234/v1", "model": "qwen"})


class TestChatSessionLocalModel:
    """Tests for explicit endpoint:<name> routing in ChatSession.start()."""

    @pytest.mark.asyncio
    async def test_named_local_endpoint_uses_configured_generation_endpoint(self) -> None:
        from gobby.servers.chat_session import ChatSession

        session = ChatSession(conversation_id="test-local-model")
        config = DaemonConfig(
            ai={
                "generation": {
                    "endpoints": {
                        "lm-studio": {
                            "api_base": "http://localhost:1234/v1",
                            "model": "qwen-coder-32b",
                            "api_key": "test-local-key",
                        }
                    }
                }
            }
        )
        session._config = config

        with (
            patch("gobby.servers.chat_session._find_cli_path", return_value="/usr/bin/claude"),
            patch("gobby.servers.chat_session._find_project_root", return_value=None),
            patch("gobby.servers.chat_session._build_gobby_mcp_entry", return_value={}),
            patch(
                "gobby.agents.local_model.ensure_local_model",
                new=AsyncMock(return_value="qwen-coder-32b"),
            ),
            patch("gobby.servers.chat_session.ClaudeSDKClient") as mock_sdk,
        ):
            mock_client = AsyncMock()
            mock_sdk.return_value = mock_client

            await session.start(model="endpoint:lm-studio")

            call_kwargs = mock_sdk.call_args
            options = call_kwargs.kwargs.get("options") or call_kwargs.args[0]
            assert options.model == "qwen-coder-32b"
            assert options.env.get("ANTHROPIC_BASE_URL") == "http://localhost:1234/v1"
            assert options.env.get("ANTHROPIC_AUTH_TOKEN") == "test-local-key"
            assert session.model == "endpoint:lm-studio"

    @pytest.mark.asyncio
    async def test_model_local_is_rejected(self) -> None:
        from gobby.servers.chat_session import ChatSession

        session = ChatSession(conversation_id="test-local-model")
        session._config = DaemonConfig()

        with pytest.raises(RuntimeError, match="Model 'local' has been removed"):
            await session._resolve_requested_model("local", {})


def _local_group(
    *,
    endpoint_name: str,
    provider_type: GenerationEndpointProtocol,
    provider_label: str,
    model_id: str,
    probed_tools: bool | None = None,
) -> LocalEndpointModelGroup:
    return LocalEndpointModelGroup(
        endpoint_name=endpoint_name,
        provider_type=provider_type,
        provider_label=provider_label,
        source="live",
        models=[
            {
                "value": f"endpoint:{endpoint_name}",
                "label": f"Default ({model_id})",
                "canonical_id": model_id,
                "is_default": True,
            }
        ],
        probed_tools=probed_tools,
    )


def _catalog_by_type(
    groups: list[LocalEndpointModelGroup],
) -> dict[str, dict[str, Any]]:
    entries = _local_generation_provider_entries(
        groups,
        codex_installed=True,
        codex_available=True,
        codex_unavailable_reason=None,
    )
    return {entry["provider_type"]: entry for entry in entries}


def test_failed_tool_probe_hides_routable_groups_from_web_chat() -> None:
    catalog = _catalog_by_type(
        [
            _local_group(
                endpoint_name="metal",
                provider_type="vllm",
                provider_label="vLLM",
                model_id="Qwen/Qwen2.5-7B-Instruct",
                probed_tools=False,
            ),
            _local_group(
                endpoint_name="studio",
                provider_type="lmstudio",
                provider_label="LM Studio",
                model_id="qwen-coder-32b",
                probed_tools=False,
            ),
            _local_group(
                endpoint_name="ollama",
                provider_type="ollama",
                provider_label="Ollama",
                model_id="llama3.2:latest",
                probed_tools=None,
            ),
        ]
    )

    assert catalog["vllm"]["supports_web_chat"] is False
    assert catalog["vllm"]["available"] is False
    assert "--enable-auto-tool-choice" in catalog["vllm"]["unavailable_reason"]
    assert "--tool-call-parser" in catalog["vllm"]["unavailable_reason"]
    assert "execution_provider" not in catalog["vllm"]

    assert catalog["lmstudio"]["supports_web_chat"] is False
    assert catalog["lmstudio"]["unavailable_reason"] == (
        "Tool-calling probe failed; re-activate the endpoint after enabling tool calling"
    )

    assert catalog["ollama"]["supports_web_chat"] is True
    assert catalog["ollama"]["execution_provider"] == "codex"
    assert catalog["ollama"]["unavailable_reason"] is None


def test_routable_transport_strategies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.agents.codex_oss import codex_local_transport_strategy

    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))

    assert codex_local_transport_strategy("lmstudio") == "oss"
    assert codex_local_transport_strategy("ollama") == "oss"
    assert codex_local_transport_strategy("vllm") == "config-override"
    assert codex_local_transport_strategy("openai-compatible") is None
    assert codex_oss_launch_args("lmstudio") == ["--oss", "--local-provider", "lmstudio"]
    assert codex_oss_launch_args("ollama") == ["--oss", "--local-provider", "ollama"]

    catalog = _catalog_by_type(
        [
            _local_group(
                endpoint_name="metal",
                provider_type="vllm",
                provider_label="vLLM",
                model_id="Qwen/Qwen2.5-7B-Instruct",
            ),
            _local_group(
                endpoint_name="studio",
                provider_type="lmstudio",
                provider_label="LM Studio",
                model_id="qwen-coder-32b",
            ),
            _local_group(
                endpoint_name="ollama",
                provider_type="ollama",
                provider_label="Ollama",
                model_id="llama3.2:latest",
            ),
            _local_group(
                endpoint_name="generic",
                provider_type="openai-compatible",
                provider_label="OpenAI Compatible",
                model_id="qwen-coder-32b",
            ),
        ]
    )
    assert catalog["vllm"]["supports_web_chat"] is True
    assert catalog["vllm"]["execution_provider"] == "codex"
    assert catalog["lmstudio"]["supports_web_chat"] is True
    assert catalog["lmstudio"]["execution_provider"] == "codex"
    assert catalog["ollama"]["supports_web_chat"] is True
    assert catalog["ollama"]["execution_provider"] == "codex"
    assert catalog["openai-compatible"]["supports_web_chat"] is False
    assert "execution_provider" not in catalog["openai-compatible"]
    assert catalog["openai-compatible"]["unavailable_reason"] == (
        "Generic OpenAI-compatible endpoints are unavailable for web chat"
    )

    config = DaemonConfig(
        web_chat_sandbox={"enabled": False},
        ai={
            "generation": {
                "endpoints": {
                    "metal": {
                        "protocol": "vllm",
                        "api_base": "http://127.0.0.1:8000/v1",
                        "model": "auto",
                    },
                    "studio": {
                        "protocol": "lmstudio",
                        "api_base": "http://localhost:1234/v1",
                        "model": "qwen-coder-32b",
                    },
                    "ollama": {
                        "protocol": "ollama",
                        "api_base": "http://localhost:11434",
                        "model": "llama3.2:latest",
                    },
                    "generic": {
                        "protocol": "openai-compatible",
                        "api_base": "http://localhost:9000/v1",
                        "model": "qwen-coder-32b",
                    },
                }
            }
        },
    )
    manager = WebChatRuntimeManager(codex_client=MagicMock(), daemon_config=config)

    vllm_backend = manager._codex_endpoint_backends["metal"]
    assert vllm_backend.client is not None
    assert vllm_backend.client._global_args == ()
    overrides = vllm_backend.client._config_overrides
    assert 'model_provider="gobby-vllm-metal"' in overrides
    assert 'model_providers.gobby-vllm-metal.wire_api="chat"' in overrides
    assert 'model_providers.gobby-vllm-metal.name="vLLM (metal)"' in overrides
    assert not any(item.startswith("model=") for item in overrides)
    assert "auto" not in repr(overrides)
    assert "--oss" not in vllm_backend.client._global_args

    studio_backend = manager._codex_endpoint_backends["studio"]
    assert studio_backend.client is not None
    assert studio_backend.client._global_args == (
        "--oss",
        "--local-provider",
        "lmstudio",
    )
    ollama_backend = manager._codex_endpoint_backends["ollama"]
    assert ollama_backend.client is not None
    assert ollama_backend.client._global_args == (
        "--oss",
        "--local-provider",
        "ollama",
    )
    assert "generic" not in manager._codex_endpoint_backends

    health = manager.health("endpoint:metal")
    assert health.provider == "endpoint:metal"
    assert health.startup_error != "unknown"

    session = manager.create_session(
        provider="codex",
        conversation_id="conv-vllm",
        model="endpoint:metal/Qwen/Qwen2.5-7B-Instruct",
    )
    assert isinstance(session, CodexManagedChatSession)
    assert session._backend is vllm_backend
    assert session._model == "Qwen/Qwen2.5-7B-Instruct"


def test_vllm_env_key_credential_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    secret = "sk-vllm-never-in-argv"

    authenticated = GenerationEndpointConfig(
        protocol="vllm",
        api_base="http://127.0.0.1:8000/v1",
        model="Qwen/Qwen2.5-7B-Instruct",
        api_key=secret,
    )
    overrides = codex_endpoint_config_overrides("metal", authenticated)
    serialized = "\n".join(overrides)

    assert secret not in serialized
    assert 'model_provider="gobby-vllm-metal"' in serialized
    assert 'model="Qwen/Qwen2.5-7B-Instruct"' in serialized
    assert 'model_providers.gobby-vllm-metal.wire_api="chat"' in serialized
    assert f'model_providers.gobby-vllm-metal.env_key="{CODEX_ENDPOINT_API_KEY_ENV}"' in serialized
    assert f'shell_environment_policy.exclude=["{CODEX_ENDPOINT_API_KEY_ENV}"]' in serialized

    env = codex_endpoint_app_server_env("metal", authenticated)
    assert env[CODEX_ENDPOINT_API_KEY_ENV] == secret
    assert "CODEX_HOME" in env
    assert secret not in env["CODEX_HOME"]

    unauthenticated = authenticated.model_copy(update={"api_key": None})
    open_overrides = codex_endpoint_config_overrides("metal", unauthenticated)
    open_serialized = "\n".join(open_overrides)
    assert "env_key" not in open_serialized
    assert "shell_environment_policy.exclude" not in open_serialized
    open_env = codex_endpoint_app_server_env("metal", unauthenticated)
    assert CODEX_ENDPOINT_API_KEY_ENV not in open_env

    config = DaemonConfig(
        web_chat_sandbox={"enabled": False},
        ai={
            "generation": {
                "endpoints": {
                    "metal": {
                        "protocol": "vllm",
                        "api_base": "http://127.0.0.1:8000/v1",
                        "model": "Qwen/Qwen2.5-7B-Instruct",
                        "api_key": secret,
                    }
                }
            }
        },
    )
    manager = WebChatRuntimeManager(codex_client=MagicMock(), daemon_config=config)
    backend = manager._codex_endpoint_backends["metal"]
    assert backend.client is not None
    argv_like = " ".join(backend.client._config_overrides)
    assert secret not in argv_like
    assert secret not in repr(backend.client._config_overrides)
    assert backend.client._env_overrides[CODEX_ENDPOINT_API_KEY_ENV] == secret
    assert secret in backend.client._redacted_env_values
