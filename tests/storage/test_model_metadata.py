"""Tests for strict context-window lookups in model metadata storage."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from gobby.config.ai import AIConfig
from gobby.providers.capabilities.metadata_aliases import (
    DEFAULT_MODEL_METADATA_ALIASES,
    MODEL_METADATA_ALIASES_KEY,
    seed_model_metadata_aliases,
)
from gobby.storage import model_metadata
from gobby.storage.model_metadata import ModelMetadataStore


def test_populate_dedupes_shared_model_ids_keeping_larger_context_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.llm.model_registry import ModelInfo

    monkeypatch.setattr(model_metadata, "_stale_warning_emitted", True)
    db = MagicMock()
    connection = db.transaction.return_value.__enter__.return_value
    models = [
        ModelInfo(
            id="anthropic/shared-model",
            name="Shared Claude",
            context_length=200_000,
            max_completion_tokens=8_000,
        ),
        ModelInfo(
            id="shared-model",
            name="Shared (smaller tier)",
            context_length=128_000,
            max_completion_tokens=4_000,
        ),
    ]

    assert ModelMetadataStore(db).populate(models) == 1

    rows = connection.executemany.call_args.args[1]
    assert rows == [("shared-model", 200_000, 8_000, "registry")]
    assert model_metadata._stale_warning_emitted is False


def test_empty_populate_retains_cached_metadata() -> None:
    db = MagicMock()

    assert ModelMetadataStore(db).populate([]) == 0

    db.transaction.assert_not_called()


def test_stale_metadata_warns_once(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_metadata, "_stale_warning_emitted", False)
    db = MagicMock()
    db.fetchone.return_value = {
        "context_length": 200_000,
        "metadata_updated_at": datetime.now(UTC) - timedelta(hours=49),
    }
    store = ModelMetadataStore(db)

    with caplog.at_level(logging.WARNING, logger="gobby.storage.model_metadata"):
        assert store.get_context_window("gpt-valid") == 200_000
        assert store.get_context_window("gpt-valid") == 200_000

    warnings = [record for record in caplog.records if "older than 48 hours" in record.message]
    assert len(warnings) == 1


def test_exact_positive_context_window_wins_without_prefix_lookup() -> None:
    db = MagicMock()
    db.fetchone.return_value = {"context_length": 200_000}

    result = ModelMetadataStore(db).get_context_window("gpt-valid")

    assert result == 200_000
    assert db.fetchone.call_count == 1
    assert "context_length > 0" in db.fetchone.call_args.args[0]


def test_vendor_prefixed_lookup_normalizes_to_bare_model() -> None:
    db = MagicMock()
    db.fetchone.return_value = {"context_length": 200_000}

    result = ModelMetadataStore(db).get_context_window("anthropic/shared-model")

    assert result == 200_000
    query, params = db.fetchone.call_args.args
    assert "provider" not in query
    assert "WHERE model = %s" in query
    assert params == ("shared-model",)


def test_endpoint_prefixed_lookup_normalizes_to_bare_model() -> None:
    db = MagicMock()
    db.fetchone.return_value = {"context_length": 200_000}

    result = ModelMetadataStore(db).get_context_window("endpoint:fast/openai/gpt-5.4")

    assert result == 200_000
    _query, params = db.fetchone.call_args.args
    assert params == ("gpt-5.4",)


def test_versioned_lookup_does_not_use_prefix_matching() -> None:
    db = MagicMock()
    db.fetchone.return_value = None

    result = ModelMetadataStore(db).get_context_window("anthropic/shared-model-versioned")

    assert result is None
    assert db.fetchone.call_count == 1
    query, params = db.fetchone.call_args.args
    assert "provider" not in query
    assert "WHERE model = %s" in query
    assert params == ("shared-model-versioned",)


@pytest.mark.parametrize(
    "invalid_value",
    [
        pytest.param(None, id="null"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(True, id="bool"),
        pytest.param("malformed", id="string"),
        pytest.param(1.5, id="float"),
    ],
)
def test_invalid_exact_row_is_rejected(invalid_value: object) -> None:
    db = MagicMock()
    db.fetchone.return_value = {"context_length": invalid_value}

    result = ModelMetadataStore(db).get_context_window("gpt-family-versioned")

    assert result is None
    assert db.fetchone.call_count == 1
    assert "context_length > 0" in db.fetchone.call_args.args[0]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("provider", " ", id="blank-provider"),
        pytest.param("provider_model_id", "", id="blank-provider-model"),
        pytest.param("openrouter_model_id", "\t", id="blank-openrouter-model"),
    ],
)
def test_model_metadata_alias_rejects_blank_fields(field: str, value: str) -> None:
    alias = {
        "provider": "synthetic-provider",
        "provider_model_id": "provider-model",
        "openrouter_model_id": "vendor/registry-model",
    }
    alias[field] = value

    with pytest.raises(ValidationError, match="must not be blank"):
        AIConfig(model_metadata_aliases=[alias])


def test_model_metadata_alias_rejects_duplicate_normalized_source_keys() -> None:
    with pytest.raises(ValidationError, match="duplicate model metadata alias source"):
        AIConfig(
            model_metadata_aliases=[
                {
                    "provider": " Synthetic-Provider ",
                    "provider_model_id": " Provider-Model ",
                    "openrouter_model_id": "vendor/registry-model-a",
                },
                {
                    "provider": "synthetic-provider",
                    "provider_model_id": "provider-model",
                    "openrouter_model_id": "vendor/registry-model-b",
                },
            ]
        )


class _MemoryConfigStore:
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values = dict(values or {})
        self.writes: list[tuple[str, object, str]] = []

    def get(self, key: str) -> object | None:
        return self.values.get(key)

    def list_keys(self, prefix: str | None = None) -> list[str]:
        return [key for key in self.values if prefix is None or key.startswith(prefix)]

    def set(self, key: str, value: object, source: str = "user") -> None:
        self.values[key] = value
        self.writes.append((key, value, source))


def test_model_metadata_alias_seed_installs_current_corrections_when_absent() -> None:
    store = _MemoryConfigStore()

    assert seed_model_metadata_aliases(store) is True

    expected = [alias.model_dump(mode="json") for alias in DEFAULT_MODEL_METADATA_ALIASES]
    assert store.values[MODEL_METADATA_ALIASES_KEY] == expected
    assert store.writes == [(MODEL_METADATA_ALIASES_KEY, expected, "default")]


def test_model_metadata_alias_seed_preserves_operator_owned_value() -> None:
    configured: list[dict[str, str]] = []
    store = _MemoryConfigStore({MODEL_METADATA_ALIASES_KEY: configured})

    assert seed_model_metadata_aliases(store) is False
    assert store.values[MODEL_METADATA_ALIASES_KEY] is configured
    assert store.writes == []
