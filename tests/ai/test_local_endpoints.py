from __future__ import annotations

import pytest

from gobby.ai.local_endpoints import (
    parse_local_endpoint_model_selector,
    parse_local_endpoint_selector,
    resolve_local_generation_endpoint_selector,
)
from gobby.config.app import DaemonConfig

pytestmark = pytest.mark.unit


def test_parse_local_endpoint_selector_preserves_endpoint_default_contract() -> None:
    assert parse_local_endpoint_selector("local:ollama") == "ollama"


def test_parse_local_endpoint_model_selector_preserves_slashed_model_ids() -> None:
    parsed = parse_local_endpoint_model_selector("local:lm-studio/google/gemma-4-26b-a4b-qat")

    assert parsed is not None
    assert parsed.endpoint_name == "lm-studio"
    assert parsed.model == "google/gemma-4-26b-a4b-qat"


def test_parse_local_endpoint_model_selector_rejects_empty_model() -> None:
    with pytest.raises(ValueError, match="local:<endpoint>/<model-id>"):
        parse_local_endpoint_model_selector("local:ollama/")


def test_resolve_local_generation_endpoint_selector_applies_selected_model() -> None:
    config = DaemonConfig(
        ai={
            "generation": {
                "local": {
                    "endpoints": {
                        "ollama": {
                            "provider": "ollama",
                            "api_base": "http://localhost:11434",
                            "model": "llama3.2",
                        }
                    }
                }
            }
        }
    )

    selection = resolve_local_generation_endpoint_selector(
        config, "local:ollama/qwen3-coder:latest"
    )

    assert selection is not None
    assert selection.name == "ollama"
    assert selection.selected_model == "qwen3-coder:latest"
    assert selection.endpoint.model == "llama3.2"
    assert selection.endpoint_with_selected_model().model == "qwen3-coder:latest"
