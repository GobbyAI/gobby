"""Contract tests for the ``ai.decisions`` configuration."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from gobby.ai import AICapability, build_daemon_ai_capability_registry
from gobby.config.ai import AIConfig, DecisionsConfig
from gobby.config.app import DaemonConfig
from gobby.config.code_index import CodeIndexCommunityLabelConfig
from gobby.config.registry import (
    CONFIG_REGISTRY,
    ConfigSecrecy,
    config_key_secrecy,
    is_removed_config_store_key,
)
from gobby.storage.config_repository import ConfigRepository, SecretBinding

pytestmark = pytest.mark.unit

WORKERS_AI_BASE = "https://api.cloudflare.com/client/v4/accounts/account-id"
REMOVED_COMMUNITY_LABEL_KEYS = (
    "decisions_api_base",
    "decisions_api_key",
    "decisions_model",
    "decisions_min_confidence",
    "decisions_timeout_seconds",
)
CONSUMERS = ("community_label", "tool_rerank", "found_work")


def _decisions(**values: Any) -> DecisionsConfig:
    return DecisionsConfig.model_validate(values)


def test_api_base_accepts_local_and_hosted() -> None:
    for api_base, wire_api, model in (
        ("http://127.0.0.1:8100", "systemone", "jev-latest"),
        ("https://openrouter.ai/api", "openrouter-decisions", "jev-latest"),
        (WORKERS_AI_BASE, "workers-ai", "clef"),
    ):
        config = _decisions(api_base=api_base, wire_api=wire_api, model=model)
        assert config.api_base == api_base

    for malformed in ("not a url", "ftp://example.com", "https://user:pw@example.com"):
        with pytest.raises(ValidationError, match="api_base"):
            _decisions(api_base=malformed, model="jev-latest")

    assert "allow_remote" not in DecisionsConfig.model_fields
    with pytest.raises(ValidationError, match="allow_remote"):
        _decisions(allow_remote=True)


def test_decisions_config_invariants() -> None:
    with pytest.raises(ValidationError, match="model"):
        _decisions(api_base="http://127.0.0.1:8100")

    for consumer in CONSUMERS:
        for evaluated in (
            {},
            {"evaluated_model": "jev-latest"},
            {"evaluated_backend": "systemone"},
        ):
            with pytest.raises(ValidationError, match="evaluated"):
                _decisions(**{consumer: {"mode": "enforce", **evaluated}})
        enforced = _decisions(
            **{
                consumer: {
                    "mode": "enforce",
                    "evaluated_model": "jev-latest",
                    "evaluated_backend": "systemone",
                }
            }
        )
        assert getattr(enforced, consumer).mode == "enforce"

    for bounds in (
        {"accept_below": 0.5, "accept_above": 0.5},
        {"accept_below": 0.9, "accept_above": 0.1},
    ):
        with pytest.raises(ValidationError, match="accept_below"):
            _decisions(found_work=bounds)

    for consumer, field in (
        ("community_label", "min_confidence"),
        ("tool_rerank", "min_probability"),
        ("found_work", "accept_below"),
        ("found_work", "accept_above"),
    ):
        for value in (-0.01, 1.01):
            with pytest.raises(ValidationError, match=field):
                _decisions(**{consumer: {field: value}})

    loaded = AIConfig.model_validate({})
    assert loaded.decisions.api_base is None
    assert [getattr(loaded.decisions, consumer).mode for consumer in CONSUMERS] == ["off"] * 3


def test_community_label_decision_keys_removed() -> None:
    for key in REMOVED_COMMUNITY_LABEL_KEYS:
        assert key not in CodeIndexCommunityLabelConfig.model_fields
        with pytest.raises(ValidationError, match=key):
            CodeIndexCommunityLabelConfig.model_validate({key: None})
        assert is_removed_config_store_key(f"code_index.community_label.{key}")


def test_decisions_scalar_rows_defaults_and_bounds() -> None:
    defaults = DecisionsConfig()

    # api_base: unset leaves the capability unavailable.
    assert defaults.api_base is None
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(ai=AIConfig(decisions=_decisions(model="jev-latest"))),
        provider_installed=lambda _entry: False,
    )
    assert registry.status(AICapability.DECIDE).available is False

    # api_key: a $secret: reference loads resolved, and the key is a masked secret.
    assert defaults.api_key is None
    key = "ai.decisions.api_key"
    assert config_key_secrecy(CONFIG_REGISTRY.resolve(key), key) is ConfigSecrecy.REFERENCE
    candidate = ConfigRepository(MagicMock()).runtime_candidate(
        {key: "$secret:DECISIONS_KEY"},
        {key: SecretBinding("$secret:DECISIONS_KEY", "resolved-key")},
    )
    assert candidate.ai.decisions.api_key == "resolved-key"

    # model: Workers AI builds its URL path from the model.
    assert defaults.model is None
    for model in ("clef", "clef-flash"):
        assert (
            _decisions(api_base=WORKERS_AI_BASE, wire_api="workers-ai", model=model).model == model
        )
    with pytest.raises(ValidationError, match="clef"):
        _decisions(api_base=WORKERS_AI_BASE, wire_api="workers-ai", model="jev-latest")

    # timeout_seconds
    assert defaults.timeout_seconds == 2.0
    with pytest.raises(ValidationError, match="timeout_seconds"):
        _decisions(timeout_seconds=0)

    # max_input_tokens
    assert defaults.max_input_tokens == 8192
    with pytest.raises(ValidationError, match="max_input_tokens"):
        _decisions(max_input_tokens=255)
    with pytest.raises(ValidationError, match="max_input_tokens"):
        _decisions(wire_api="openrouter-decisions", max_input_tokens=32000)
    with pytest.raises(ValidationError, match="max_input_tokens"):
        _decisions(backend_max_state_tokens=10000, max_input_tokens=10001)

    # backend_max_state_tokens resolves per wire unless set explicitly.
    assert defaults.backend_max_state_tokens is None
    for wire_api, resolved in (
        ("systemone", 65536),
        ("workers-ai", 65536),
        ("openrouter-decisions", 32000),
    ):
        assert _decisions(wire_api=wire_api).resolved_backend_max_state_tokens == resolved
    explicit = _decisions(backend_max_state_tokens=100000)
    assert explicit.backend_max_state_tokens == 100000
    assert explicit.resolved_backend_max_state_tokens == 100000
    with pytest.raises(ValidationError, match="backend_max_state_tokens"):
        _decisions(backend_max_state_tokens=0)

    # failure_cooldown_seconds
    assert defaults.failure_cooldown_seconds == 60
    with pytest.raises(ValidationError, match="failure_cooldown_seconds"):
        _decisions(failure_cooldown_seconds=-1)

    # wire_api
    assert defaults.wire_api == "systemone"
    for wire_api in ("openrouter-decisions", "workers-ai"):
        assert _decisions(wire_api=wire_api).wire_api == wire_api
    for rejected in ("openai-decisions", "chat-completions"):
        with pytest.raises(ValidationError, match="wire_api"):
            _decisions(wire_api=rejected)

    # identity_contract: only a /v1/systemone server exposes the model card.
    assert defaults.identity_contract is None
    for wire_api in ("systemone", "openrouter-decisions", "workers-ai"):
        config = _decisions(wire_api=wire_api, identity_contract="response_version")
        assert config.identity_contract == "response_version"
    assert _decisions(identity_contract="model_card").identity_contract == "model_card"
    for wire_api in ("openrouter-decisions", "workers-ai"):
        with pytest.raises(ValidationError, match="model_card"):
            _decisions(wire_api=wire_api, identity_contract="model_card")


def test_decisions_consumer_rows_defaults() -> None:
    defaults = DecisionsConfig()

    assert defaults.community_label.model_dump() == {
        "mode": "off",
        "min_confidence": 0.5,
        "evaluated_model": None,
        "evaluated_backend": None,
    }
    assert defaults.tool_rerank.model_dump() == {
        "mode": "off",
        "min_probability": 0.5,
        "evaluated_model": None,
        "evaluated_backend": None,
    }
    assert defaults.found_work.model_dump() == {
        "mode": "off",
        "accept_below": 0.1,
        "accept_above": 0.9,
        "evaluated_model": None,
        "evaluated_backend": None,
    }
    for consumer in CONSUMERS:
        with pytest.raises(ValidationError, match="evaluated_model"):
            _decisions(**{consumer: {"mode": "enforce"}})
        assert _decisions(**{consumer: {"mode": "shadow"}}).model_dump()[consumer]["mode"] == (
            "shadow"
        )
