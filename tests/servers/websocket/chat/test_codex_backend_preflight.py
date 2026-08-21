"""Codex web-chat backend pre-flight diagnostics."""

from __future__ import annotations

import pytest

from gobby.agents.local_model import LocalModelError
from gobby.config.ai import GenerationEndpointConfig
from gobby.servers.websocket.chat.backends.codex import local_model_preflight_message

pytestmark = pytest.mark.unit


def test_preflight_message_keeps_resolver_diagnostic() -> None:
    endpoint = GenerationEndpointConfig(
        protocol="vllm",
        api_base="http://127.0.0.1:8000/v1",
        model="auto",
    )
    error = LocalModelError(
        "model: auto requires exactly one served vLLM model; found 2: a-model, b-model"
    )

    message = local_model_preflight_message(endpoint, error)

    assert "protocol=vllm" in message
    assert "model=auto" in message
    assert "found 2: a-model, b-model" in message
