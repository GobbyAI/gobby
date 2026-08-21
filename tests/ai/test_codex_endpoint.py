"""Tests for scoped Codex Responses endpoint configuration."""

from pathlib import Path

import pytest

from gobby.ai.codex_endpoint import (
    CODEX_ENDPOINT_API_KEY_ENV,
    codex_endpoint_app_server_env,
    codex_endpoint_config_overrides,
    codex_endpoint_env,
    codex_endpoint_provider_id,
)
from gobby.config.ai import GenerationEndpointConfig

pytestmark = pytest.mark.unit


def _endpoint(api_key: str = "sk-test-secret") -> GenerationEndpointConfig:
    return GenerationEndpointConfig(
        wire_api="responses",
        api_base="https://openrouter.ai/api/v1",
        api_key=api_key,
        model="moonshotai/kimi-k3",
        tool_chat=True,
    )


def test_openrouter_overrides_use_stable_secret_free_codex_provider() -> None:
    overrides = codex_endpoint_config_overrides("openrouter", _endpoint())

    assert codex_endpoint_provider_id("openrouter") == "gobby_endpoint_openrouter"
    assert overrides == (
        'model_provider="gobby_endpoint_openrouter"',
        'model="moonshotai/kimi-k3"',
        'model_providers.gobby_endpoint_openrouter.name="OpenRouter"',
        'model_providers.gobby_endpoint_openrouter.base_url="https://openrouter.ai/api/v1"',
        (f'model_providers.gobby_endpoint_openrouter.env_key="{CODEX_ENDPOINT_API_KEY_ENV}"'),
        'model_providers.gobby_endpoint_openrouter.wire_api="responses"',
        f'shell_environment_policy.exclude=["{CODEX_ENDPOINT_API_KEY_ENV}"]',
        "features.shell_snapshot=false",
    )
    assert "sk-test-secret" not in repr(overrides)


def test_endpoint_key_is_exposed_only_through_child_environment() -> None:
    assert codex_endpoint_env(_endpoint()) == {CODEX_ENDPOINT_API_KEY_ENV: "sk-test-secret"}


def test_endpoint_app_server_uses_isolated_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))

    env = codex_endpoint_app_server_env("openrouter", _endpoint())
    second_env = codex_endpoint_app_server_env("backup", _endpoint())

    assert env == {
        CODEX_ENDPOINT_API_KEY_ENV: "sk-test-secret",
        "CODEX_HOME": str(tmp_path / "codex-endpoints" / "openrouter"),
    }
    assert second_env["CODEX_HOME"] == str(tmp_path / "codex-endpoints" / "backup")
    assert (tmp_path / "codex-endpoints" / "openrouter").is_dir()
    assert (tmp_path / "codex-endpoints" / "backup").is_dir()


@pytest.mark.parametrize("api_key", ["", "$secret:OPENROUTER_API_KEY"])
def test_endpoint_environment_rejects_unresolved_secret(api_key: str) -> None:
    with pytest.raises(ValueError, match="referenced secret"):
        codex_endpoint_env(_endpoint(api_key))


def test_codex_overrides_reject_chat_completions_endpoint() -> None:
    endpoint = _endpoint().model_copy(update={"wire_api": "chat-completions"})

    with pytest.raises(ValueError, match="wire_api='responses'"):
        codex_endpoint_config_overrides("openrouter", endpoint)


@pytest.mark.parametrize(
    "api_base",
    [
        pytest.param("http://127.0.0.1:8000", id="bare-origin"),
        pytest.param("http://127.0.0.1:8000/v1/", id="v1-trailing-slash"),
    ],
)
def test_vllm_override_base_url_is_normalized(api_base: str) -> None:
    endpoint = GenerationEndpointConfig(protocol="vllm", api_base=api_base, model="served-model")

    overrides = codex_endpoint_config_overrides("metal", endpoint)

    assert 'model_providers.gobby-vllm-metal.base_url="http://127.0.0.1:8000/v1"' in overrides
