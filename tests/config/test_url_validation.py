"""Tests for shared endpoint URL validation across configuration models."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import BaseModel, ValidationError

from gobby.config.ai import GenerationEndpointConfig
from gobby.config.communications import CommunicationsConfig
from gobby.config.extensions import WebhookEndpointConfig
from gobby.config.persistence import EmbeddingsConfig, QdrantConfig
from gobby.config.skills import HubConfig
from gobby.config.tasks import TaskValidationConfig
from gobby.config.url_validation import validate_endpoint_url
from gobby.config.voice import OpenAICompatibleAudioBindingConfig
from gobby.mcp_proxy.models import MCPServerConfig

pytestmark = pytest.mark.unit

UrlModelFactory = Callable[[str], BaseModel]


def _local_generation_config(url: str) -> BaseModel:
    return GenerationEndpointConfig(api_base=url, model="local-model")


def _communications_config(url: str) -> BaseModel:
    return CommunicationsConfig(webhook_base_url=url)


def _webhook_config(url: str) -> BaseModel:
    return WebhookEndpointConfig(name="validation-test", url=url)


def _qdrant_config(url: str) -> BaseModel:
    return QdrantConfig(url=url)


def _embeddings_config(url: str) -> BaseModel:
    return EmbeddingsConfig(api_base=url)


def _hub_config(url: str) -> BaseModel:
    return HubConfig(type="skillsmp", base_url=url)


def _task_validation_config(url: str) -> BaseModel:
    return TaskValidationConfig(escalation_webhook_url=url)


def _audio_binding_config(url: str) -> BaseModel:
    return OpenAICompatibleAudioBindingConfig(
        provider="local-audio",
        url=url,
        model="audio-model",
    )


HTTP_URL_MODEL_FACTORIES: tuple[UrlModelFactory, ...] = (
    _local_generation_config,
    _communications_config,
    _webhook_config,
    _qdrant_config,
    _embeddings_config,
    _hub_config,
    _task_validation_config,
    _audio_binding_config,
)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://localhost:8080/v1", id="http-localhost"),
        pytest.param("https://api.example.com/v1?q=value#result", id="https-host"),
        pytest.param("https://[::1]:8443/v1", id="https-ipv6"),
    ],
)
def test_validate_endpoint_url_accepts_http_urls_with_hosts(url: str) -> None:
    assert validate_endpoint_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("", id="empty"),
        pytest.param("localhost:8080/v1", id="missing-scheme"),
        pytest.param("ftp://api.example.com/v1", id="unsupported-scheme"),
        pytest.param("https:///v1", id="missing-host"),
        pytest.param("https://bad host/v1", id="whitespace-in-host"),
        pytest.param("https://api.example.com:invalid/v1", id="invalid-port"),
        pytest.param("https://user:token@vllm.internal/v1", id="embedded-credentials"),
        pytest.param("https://token@vllm.internal/v1", id="embedded-username"),
    ],
)
def test_validate_endpoint_url_rejects_invalid_scheme_or_host(url: str) -> None:
    with pytest.raises(ValueError):
        validate_endpoint_url(url)


@pytest.mark.parametrize(
    "factory",
    [pytest.param(factory, id=factory.__name__) for factory in HTTP_URL_MODEL_FACTORIES],
)
def test_http_url_config_models_share_scheme_and_host_validation(
    factory: UrlModelFactory,
) -> None:
    with pytest.raises(ValidationError):
        factory("ftp://example.com/endpoint")


def test_optional_http_url_config_fields_preserve_unset_values() -> None:
    assert QdrantConfig(url=None).url is None
    assert EmbeddingsConfig(api_base=None).api_base is None
    assert HubConfig(type="skillsmp", base_url=None).base_url is None
    assert TaskValidationConfig(escalation_webhook_url=None).escalation_webhook_url is None
    assert CommunicationsConfig(webhook_base_url="").webhook_base_url == ""


@pytest.mark.parametrize(
    ("transport", "url"),
    [
        pytest.param("http", "https://mcp.example.com/rpc", id="http"),
        pytest.param("sse", "http://localhost:8080/events", id="sse"),
        pytest.param("websocket", "wss://mcp.example.com/ws", id="websocket"),
    ],
)
def test_mcp_server_config_accepts_transport_appropriate_url(
    transport: str,
    url: str,
) -> None:
    config = MCPServerConfig(
        name="url-validation",
        project_id="project-id",
        transport=transport,
        url=url,
    )

    config.validate()

    assert config.url == url


@pytest.mark.parametrize(
    ("transport", "url"),
    [
        pytest.param("http", "ws://localhost:8080/rpc", id="http-with-ws"),
        pytest.param("sse", "events.example.com", id="sse-without-scheme"),
        pytest.param("websocket", "https://mcp.example.com/ws", id="websocket-with-http"),
    ],
)
def test_mcp_server_config_rejects_transport_inappropriate_url(
    transport: str,
    url: str,
) -> None:
    config = MCPServerConfig(
        name="url-validation",
        project_id="project-id",
        transport=transport,
        url=url,
    )

    with pytest.raises(ValueError):
        config.validate()
