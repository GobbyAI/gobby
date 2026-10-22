"""Retired close-review overrides cannot poison strict config reads after upgrade."""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest

from gobby.storage.config_repository import (
    MAX_CONFIG_REVISION,
    ConfigRepository,
    UnknownStoredConfigKeyError,
)
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "crates/gcore/assets/schema/migrations/473_drop_retired_task_validation_config.sql"
).read_text()

# Exhaustive TaskValidationConfig field removals in a94b7650bc..3ba8ff6f74.
RETIRED_FIELDS = (
    "auto_generate_on_create",
    "auto_generate_on_expand",
    "close_review_max_concurrency_per_project",
    "close_review_min_severity",
    "close_review_prompt_max_chars",
    "close_review_total_timeout_seconds",
    "close_review_validator_timeout_seconds",
    "close_validation_escalation_threshold",
    "criteria_prompt_path",
    "criteria_system_prompt",
    "escalation_enabled",
    "escalation_notify",
    "escalation_webhook_url",
    "max_iterations",
    "require_clean_attributed_paths_on_close",
)
LIVE_RETIRED_FIELDS = (
    "close_review_max_concurrency_per_project",
    "close_review_min_severity",
    "close_review_prompt_max_chars",
    "close_review_validator_timeout_seconds",
    "criteria_system_prompt",
)
SURVIVING_OVERRIDES = {
    "gobby-tasks.validation.candidates": ["claude/sonnet"],
    "session_feedback.review.candidates": ["claude/sonnet"],
    "gobby-tasks.validation.enabled": False,
    "memory.enabled": False,
}


@pytest.mark.parametrize("fields", [RETIRED_FIELDS, LIVE_RETIRED_FIELDS], ids=["all", "live"])
def test_retired_rows_migrate_to_strict_readable_config(
    postgres_db: HubDatabase, fields: tuple[str, ...]
) -> None:
    with postgres_db.transaction() as transaction:
        for field in fields:
            transaction.execute(
                "INSERT INTO config_store (key, value) VALUES (%s, %s)",
                (f"gobby-tasks.validation.{field}", "null"),
            )
        for key, value in SURVIVING_OVERRIDES.items():
            transaction.execute(
                "INSERT INTO config_store (key, value) VALUES (%s, %s)",
                (key, json.dumps(value)),
            )
    repository = ConfigRepository(postgres_db)
    revision = repository.current_revision()
    with pytest.raises(UnknownStoredConfigKeyError):
        repository.read(resolve_secrets=False)

    with postgres_db.transaction() as transaction:
        transaction.execute(MIGRATION)
    snapshot = repository.read(resolve_secrets=False)
    assert snapshot.revision == revision + 1
    for key, value in SURVIVING_OVERRIDES.items():
        assert snapshot.values[key] == value
    for field in RETIRED_FIELDS:
        assert f"gobby-tasks.validation.{field}" not in snapshot.values

    with postgres_db.transaction() as transaction:
        transaction.execute(MIGRATION)
    assert repository.read(resolve_secrets=False).revision == snapshot.revision


def test_empty_retirement_does_not_advance_config_revision(postgres_db: HubDatabase) -> None:
    repository = ConfigRepository(postgres_db)
    before = repository.read(resolve_secrets=False)
    with postgres_db.transaction() as transaction:
        transaction.execute(MIGRATION)
    assert repository.read(resolve_secrets=False) == before


def test_revision_exhaustion_preserves_retired_rows(postgres_db: HubDatabase) -> None:
    key = "gobby-tasks.validation.criteria_system_prompt"
    with postgres_db.transaction() as transaction:
        transaction.execute("INSERT INTO config_store (key, value) VALUES (%s, %s)", (key, "null"))
        transaction.execute("UPDATE config_state SET revision = %s", (MAX_CONFIG_REVISION,))
    with pytest.raises(psycopg.errors.RaiseException, match="revision exhausted"):
        with postgres_db.transaction() as transaction:
            transaction.execute(MIGRATION)
    assert postgres_db.fetchone("SELECT key FROM config_store WHERE key = %s", (key,)) is not None
    assert ConfigRepository(postgres_db).current_revision() == MAX_CONFIG_REVISION
