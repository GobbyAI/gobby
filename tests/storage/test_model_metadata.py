"""Tests for strict context-window lookups in model metadata storage."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from gobby.config.ai import AIConfig
from gobby.llm.model_registry import ModelInfo, ModelReasoningInfo
from gobby.storage import model_metadata
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.model_metadata import ModelMetadataStore

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_populate_dedupes_shared_model_ids_keeping_larger_context_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    assert rows == [("shared-model", 200_000, 8_000, False, None, None, None, None, "registry")]
    assert model_metadata._stale_warning_emitted is False


@pytest.mark.integration
def test_reasoning_metadata_database_round_trip(postgres_db: HubDatabase) -> None:
    store = ModelMetadataStore(postgres_db)
    models = [
        ModelInfo("vendor/absent", "Absent", 1, None),
        ModelInfo(
            "vendor/null-efforts",
            "Null efforts",
            2,
            None,
            ModelReasoningInfo(),
        ),
        ModelInfo(
            "vendor/empty-efforts",
            "Empty efforts",
            3,
            None,
            ModelReasoningInfo(supported_efforts=()),
        ),
        ModelInfo(
            "openai/gpt-5.6-luna",
            "Luna",
            4,
            128_000,
            ModelReasoningInfo(
                supported_efforts=("max", "medium", "none"),
                default_effort="medium",
                default_enabled=True,
                mandatory=False,
            ),
        ),
    ]

    assert store.populate(models) == 4

    metadata = store.get_all()
    assert metadata["vendor/absent"].reasoning_present is False
    assert metadata["vendor/null-efforts"].reasoning_present is True
    assert metadata["vendor/null-efforts"].reasoning_supported_efforts is None
    assert metadata["vendor/empty-efforts"].reasoning_supported_efforts == ()
    assert metadata["gpt-5.6-luna"].reasoning_supported_efforts == (
        "max",
        "medium",
        "none",
    )
    assert metadata["gpt-5.6-luna"].reasoning_default_effort == "medium"
    assert metadata["gpt-5.6-luna"].reasoning_default_enabled is True
    assert metadata["gpt-5.6-luna"].reasoning_mandatory is False


@pytest.mark.integration
def test_reasoning_migration_preserves_existing_rows(postgres_db: HubDatabase) -> None:
    for column in (
        "reasoning_present",
        "reasoning_supported_efforts",
        "reasoning_default_effort",
        "reasoning_default_enabled",
        "reasoning_mandatory",
    ):
        postgres_db.execute(f"ALTER TABLE model_metadata DROP COLUMN IF EXISTS {column}")
    postgres_db.execute(
        "INSERT INTO model_metadata (model, context_length, source) VALUES (%s, %s, %s)",
        ("existing-model", 32_000, "registry"),
    )

    migration = (
        _REPO_ROOT / "crates/gcore/assets/schema/migrations/401_model_metadata_reasoning.sql"
    ).read_text(encoding="utf-8")
    postgres_db.execute(migration)

    row = postgres_db.fetchone(
        "SELECT reasoning_present, reasoning_supported_efforts, reasoning_default_effort, "
        "reasoning_default_enabled, reasoning_mandatory FROM model_metadata "
        "WHERE model = %s",
        ("existing-model",),
    )
    assert row is not None
    assert all(value is None for value in row.values())


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


def test_stale_reasoning_metadata_remains_available(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_metadata, "_stale_warning_emitted", False)
    db = MagicMock()
    db.fetchone.return_value = {
        "context_length": 200_000,
        "max_completion_tokens": 8_000,
        "reasoning_present": True,
        "reasoning_supported_efforts": ["low", "medium"],
        "reasoning_default_effort": "medium",
        "reasoning_default_enabled": True,
        "reasoning_mandatory": False,
        "metadata_updated_at": datetime.now(UTC) - timedelta(hours=49),
    }

    with caplog.at_level(logging.WARNING, logger="gobby.storage.model_metadata"):
        metadata = ModelMetadataStore(db).get_model_metadata("gpt-stale")

    assert metadata is not None
    assert metadata.reasoning_default_effort == "medium"
    assert "older than 48 hours" in caplog.text


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
