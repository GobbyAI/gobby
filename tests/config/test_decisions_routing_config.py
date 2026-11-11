"""Per-type decision routing: override entries, endpoint_for, and load-time wire refusals."""

from typing import Any

import pytest
from pydantic import ValidationError

from gobby.config.ai import DecisionEndpointConfig, DecisionKind, DecisionsConfig

pytestmark = pytest.mark.unit


def _routed(config: DecisionsConfig, kind: DecisionKind) -> DecisionEndpointConfig:
    endpoint = config.endpoint_for(kind)
    assert endpoint is not None, f"{kind} is unrouted"
    return endpoint


OLLAMA: dict[str, Any] = {
    "wire_api": "systemone",
    "api_base": "http://127.0.0.1:11434",
    "model": "clef:27b",
}
OPENROUTER_BOX: dict[str, Any] = {
    "wire_api": "openrouter-decisions",
    "api_base": "https://openrouter.ai/api",
    "model": "typesafe/jev-1.14",
    "api_key": "$secret:openrouter",
}
WORKERS_AI: dict[str, Any] = {
    "wire_api": "workers-ai",
    "api_base": "https://api.cloudflare.com/client/v4/accounts/account-id",
    "model": "clef",
}


def test_choice_noul_score_on_systemone_and_box_on_openrouter_load() -> None:
    config = DecisionsConfig(
        overrides={"choice": OLLAMA, "noul": OLLAMA, "score": OLLAMA, "box": OPENROUTER_BOX},
    )

    assert _routed(config, "choice").wire_api == "systemone"
    assert _routed(config, "noul").model == "clef:27b"
    assert _routed(config, "score").api_base == "http://127.0.0.1:11434"
    assert _routed(config, "box").wire_api == "openrouter-decisions"
    assert _routed(config, "box").model == "typesafe/jev-1.14"


def test_type_without_entry_uses_the_default_endpoint() -> None:
    config = DecisionsConfig(**OLLAMA, overrides={"box": OPENROUTER_BOX})

    assert config.endpoint_for("choice") is config
    assert config.endpoint_for("noul") is config
    assert config.endpoint_for("score") is config
    assert _routed(config, "box").wire_api == "openrouter-decisions"


def test_no_overrides_loads_the_single_default_endpoint_unchanged() -> None:
    config = DecisionsConfig(**OLLAMA)

    for kind in ("choice", "noul", "score", "box"):
        assert config.endpoint_for(kind) is config


def test_unset_default_without_entries_routes_nothing() -> None:
    config = DecisionsConfig()

    for kind in ("choice", "noul", "score", "box"):
        assert config.endpoint_for(kind) is None


@pytest.mark.parametrize("entry", [OLLAMA, WORKERS_AI], ids=["systemone", "workers-ai"])
def test_box_entry_on_a_wire_that_cannot_serve_it_is_refused_at_load(
    entry: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError) as caught:
        DecisionsConfig(overrides={"box": entry})

    message = str(caught.value)
    assert "box" in message
    assert f"'{entry['wire_api']}'" in message
    assert "openrouter-decisions" in message


def test_bare_default_on_systemone_loads_and_box_routes_to_the_default() -> None:
    # A default with no entries keeps loading; the service refuses Box before dialing.
    config = DecisionsConfig(**OLLAMA)

    assert config.endpoint_for("box") is config


def test_override_entry_needs_api_base_and_model() -> None:
    with pytest.raises(ValidationError, match="overrides.box needs api_base and model"):
        DecisionsConfig(overrides={"box": {"wire_api": "openrouter-decisions"}})


def test_an_all_default_override_entry_is_no_entry() -> None:
    # The config projection emits every leaf of each optional entry at its default.
    unset = DecisionEndpointConfig().model_dump()
    config = DecisionsConfig(**OLLAMA, overrides={"choice": unset, "box": unset})

    assert config.overrides.choice is None
    assert config.overrides.box is None
    assert config.endpoint_for("choice") is config


def test_override_entries_run_the_endpoint_validators() -> None:
    with pytest.raises(ValidationError, match="long_context requires systemone"):
        DecisionsConfig(overrides={"choice": {**WORKERS_AI, "long_context": True}})


def test_override_keys_are_limited_to_the_four_decision_types() -> None:
    with pytest.raises(ValidationError):
        DecisionsConfig(overrides={"found_work": OPENROUTER_BOX})
