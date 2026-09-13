"""Configuration normalization for endpoint-scoped local context routes."""

from dataclasses import replace

import pytest

from gobby.config.app import DaemonConfig
from gobby.providers.capabilities.local_context import build_context_observation
from gobby.providers.capabilities.local_context_config import (
    cli_endpoint_route,
    configured_local_routes,
)

pytestmark = pytest.mark.unit


def test_configured_local_routes() -> None:
    config = DaemonConfig(
        ai={
            "generation": {
                "endpoints": {
                    "studio": {
                        "protocol": "lmstudio",
                        "api_base": "http://LOCALHOST:1234/v1/?private=query",
                        "model": "configured-model",
                        "api_key": "studio-secret",
                    },
                    "vllm": {
                        "protocol": "vllm",
                        "api_base": "http://localhost:8000",
                        "model": "vllm-default",
                        "api_key": "vllm-secret",
                    },
                    "remote": {
                        "api_base": "https://models.example.test/v1",
                        "model": "remote-model",
                    },
                }
            }
        },
        session_summary={
            "candidates": [
                "grok/endpoint:studio/grok-model",
                "droid/endpoint:studio/droid-model",
            ]
        },
    )
    routes = configured_local_routes(
        config,
        machine_id="machine-a",
        environment={"VLLM_TOKEN": "vllm-secret", "QWEN_API_KEY": "qwen-secret"},
        codex_config={
            "model": "codex-model",
            "model_provider": "active-local",
            "model_providers": {
                "active-local": {
                    "base_url": "http://localhost:8000/v1",
                    "env_key": "VLLM_TOKEN",
                },
                "unused-remote": {"base_url": "https://models.example.test/v1"},
            },
        },
        claude_settings={
            "model": "claude-remote",
            "env": {"ANTHROPIC_BASE_URL": "https://models.example.test/v1"},
        },
        qwen_settings={
            "model": {"name": "qwen-model"},
            "modelProviders": {
                "openai": [
                    {
                        "id": "qwen-model",
                        "baseUrl": "http://127.0.0.1:9000/v1?private=query",
                    }
                ]
            },
        },
    )

    by_selection = {(route.provider, route.model_id): route for route in routes}
    assert set(by_selection) == {
        ("endpoint:studio", "configured-model"),
        ("endpoint:vllm", "vllm-default"),
        ("codex", "codex-model"),
        ("droid", "droid-model"),
        ("grok", "grok-model"),
        ("qwen", "qwen-model"),
    }
    matched = by_selection["codex", "codex-model"]
    assert matched.endpoint_id == "endpoint:vllm"
    assert matched.protocol == "vllm"
    assert (
        matched.configuration_fingerprint
        == by_selection["endpoint:vllm", "vllm-default"].configuration_fingerprint
    )
    assert by_selection["qwen", "qwen-model"].endpoint_id == "cli:qwen:openai:qwen-model"
    assert all(route.is_local for route in routes)

    rendered = repr(routes) + repr([route.to_dict() for route in routes])
    assert "studio-secret" not in rendered
    assert "vllm-secret" not in rendered
    assert "qwen-secret" not in rendered
    assert "private=query" not in rendered


def test_cli_route_requires_credential_match_when_available() -> None:
    config = DaemonConfig(
        ai={
            "generation": {
                "endpoints": {
                    "first": {
                        "api_base": "http://localhost:1234/v1",
                        "api_key": "first-secret",
                        "model": "model-a",
                    },
                    "second": {
                        "api_base": "http://localhost:1234/v1/",
                        "api_key": "second-secret",
                        "model": "model-b",
                    },
                    "prefixed": {
                        "api_base": "http://localhost:1234/proxy/v1",
                        "api_key": "second-secret",
                        "model": "model-c",
                    },
                }
            }
        }
    )
    endpoints = config.ai.generation.endpoints

    matched = cli_endpoint_route(
        machine_id="machine-a",
        provider="codex",
        model_id="selected",
        api_base="http://LOCALHOST:1234/v1?ignored=true",
        api_key="second-secret",
        endpoints=endpoints,
    )
    ambiguous = cli_endpoint_route(
        machine_id="machine-a",
        provider="codex",
        model_id="selected",
        api_base="http://localhost:1234/v1",
        api_key=None,
        endpoints=endpoints,
    )

    assert matched.endpoint_id == "endpoint:second"
    assert ambiguous.endpoint_id == "cli:codex:active"
    assert matched.configuration_fingerprint != ambiguous.configuration_fingerprint


def test_route_matches_only_exact_observation_identity() -> None:
    config = DaemonConfig(
        ai={
            "generation": {
                "endpoints": {
                    "local": {
                        "api_base": "http://localhost:8000/v1",
                        "model": "model-a",
                    }
                }
            }
        }
    )
    route = configured_local_routes(
        config,
        machine_id="machine-a",
        environment={},
        codex_config={},
        claude_settings={},
        qwen_settings={},
    )[0]
    observation = build_context_observation(
        machine_id=route.machine_id,
        endpoint_id=route.endpoint_id,
        configuration_fingerprint=route.configuration_fingerprint,
        provider=route.protocol,
        model_id=route.model_id,
    )

    assert route.matches_observation(observation)
    assert not route.matches_observation(replace(observation, model_id="model-b"))
