from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from gobby.agents.spawners.command_builder import build_cli_command
from gobby.ai.codex_endpoint import CODEX_ENDPOINT_API_KEY_ENV
from gobby.config.app import DaemonConfig
from gobby.mcp_proxy.tools.spawn_agent._generation_endpoint import (
    resolve_spawn_generation_endpoint,
)

pytestmark = pytest.mark.unit

_VLLM_SECRET = "sk-vllm-spawn-never-in-argv"


def _config() -> DaemonConfig:
    return DaemonConfig(
        ai={
            "generation": {
                "endpoints": {
                    "metal": {
                        "protocol": "vllm",
                        "api_base": "http://127.0.0.1:8000/v1",
                        "model": "Qwen/Qwen2.5-7B-Instruct",
                        "api_key": _VLLM_SECRET,
                    },
                    "open-metal": {
                        "protocol": "vllm",
                        "api_base": "http://127.0.0.1:8001/v1",
                        "model": "auto",
                    },
                    "studio": {
                        "protocol": "lmstudio",
                        "api_base": "http://localhost:1234/v1",
                        "model": "qwen-coder-32b",
                    },
                    "generic": {
                        "protocol": "openai-compatible",
                        "api_base": "http://localhost:9000/v1",
                        "model": "qwen-coder-32b",
                    },
                }
            }
        }
    )


async def _resolved_model(endpoint: object, run_manager: object = None) -> str:
    model = getattr(endpoint, "model", "")
    if model.strip() == "auto":
        return "Qwen/Qwen2.5-VL-7B"
    return str(model)


@pytest.mark.asyncio
async def test_vllm_spawn_env_key_transport() -> None:
    config = _config()
    with patch(
        "gobby.agents.local_model.ensure_local_model",
        new=AsyncMock(side_effect=_resolved_model),
    ):
        vllm = await resolve_spawn_generation_endpoint(
            model="endpoint:metal/Qwen/Qwen2.5-7B-Instruct",
            api_base=None,
            api_token=None,
            daemon_config=config,
            run_manager=None,
            runtime_provider="codex",
        )
        open_vllm = await resolve_spawn_generation_endpoint(
            model="endpoint:open-metal",
            api_base=None,
            api_token=None,
            daemon_config=config,
            run_manager=None,
            runtime_provider="codex",
        )
        studio = await resolve_spawn_generation_endpoint(
            model="endpoint:studio",
            api_base=None,
            api_token=None,
            daemon_config=config,
            run_manager=None,
            runtime_provider="codex",
        )
        with pytest.raises(ValueError, match="protocol=openai-compatible"):
            await resolve_spawn_generation_endpoint(
                model="endpoint:generic",
                api_base=None,
                api_token=None,
                daemon_config=config,
                run_manager=None,
                runtime_provider="codex",
            )

    assert vllm.model == "Qwen/Qwen2.5-7B-Instruct"
    assert vllm.is_local is True
    assert vllm.codex_oss_provider is None
    assert vllm.child_env == {CODEX_ENDPOINT_API_KEY_ENV: _VLLM_SECRET}
    serialized = "\n".join(vllm.codex_config_overrides)
    assert _VLLM_SECRET not in serialized
    assert 'model_provider="gobby-vllm-metal"' in serialized
    assert 'model="Qwen/Qwen2.5-7B-Instruct"' in serialized
    assert 'model_providers.gobby-vllm-metal.wire_api="chat"' in serialized
    assert f'model_providers.gobby-vllm-metal.env_key="{CODEX_ENDPOINT_API_KEY_ENV}"' in serialized
    assert f'shell_environment_policy.exclude=["{CODEX_ENDPOINT_API_KEY_ENV}"]' in serialized

    cmd, env = build_cli_command(
        "codex",
        prompt="",
        auto_approve=True,
        working_directory="/repo",
        model=vllm.model,
        config_overrides=list(vllm.codex_config_overrides),
        env_overrides=vllm.child_env,
        codex_oss_provider=vllm.codex_oss_provider,
    )
    argv = " ".join(cmd)
    assert "--oss" not in cmd
    assert "--local-provider" not in cmd
    assert "-c" in cmd
    assert 'model_provider="gobby-vllm-metal"' in cmd
    assert 'model_providers.gobby-vllm-metal.wire_api="chat"' in cmd
    assert f'model_providers.gobby-vllm-metal.env_key="{CODEX_ENDPOINT_API_KEY_ENV}"' in cmd
    assert f'shell_environment_policy.exclude=["{CODEX_ENDPOINT_API_KEY_ENV}"]' in cmd
    assert _VLLM_SECRET not in argv
    assert _VLLM_SECRET not in repr(vllm.codex_config_overrides)
    assert env[CODEX_ENDPOINT_API_KEY_ENV] == _VLLM_SECRET

    assert open_vllm.codex_oss_provider is None
    open_serialized = "\n".join(open_vllm.codex_config_overrides)
    assert "env_key" not in open_serialized
    assert "shell_environment_policy.exclude" not in open_serialized
    assert "auto" not in open_serialized
    assert 'model="Qwen/Qwen2.5-VL-7B"' in open_serialized
    assert open_vllm.child_env in (None, {})
    open_cmd, _open_env = build_cli_command(
        "codex",
        prompt="",
        model=open_vllm.model,
        config_overrides=list(open_vllm.codex_config_overrides),
        env_overrides=open_vllm.child_env,
        codex_oss_provider=open_vllm.codex_oss_provider,
    )
    assert "--oss" not in open_cmd
    assert "auto" not in " ".join(open_cmd)

    studio_cmd, _studio_env = build_cli_command(
        "codex",
        prompt="hello",
        model=studio.model,
        config_overrides=list(studio.codex_config_overrides),
        env_overrides=studio.child_env,
        codex_oss_provider=studio.codex_oss_provider,
    )
    assert studio.codex_oss_provider == "lmstudio"
    assert studio.codex_config_overrides == ()
    assert studio_cmd[:4] == ["codex", "--oss", "--local-provider", "lmstudio"]
