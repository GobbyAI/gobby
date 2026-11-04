"""Daemon-start rule sync logs real failures but not the dry run's expected unresolved names."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.sync_registry import sync_bundled_content_to_db
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.integration

BROKEN_RULE_YAML = """\
version: "1.0.0"
rules:
  broken-expression-rule:
    description: "A rule whose condition does not parse"
    event: before_tool
    enabled: true
    when: "source =="
    effects: []
"""


def test_bundled_rule_sync_logs_no_unavailable_dry_run_warnings(
    postgres_db: PostgresHubDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    # The daemon-start sync logs every rule warning at WARNING level, so drive the
    # same registry entry point it uses for the bundled rules content type.
    with caplog.at_level(logging.WARNING):
        sync_bundled_content_to_db(postgres_db, only=["rules"])

    unavailable = [
        record.getMessage() for record in caplog.records if "is unavailable" in record.getMessage()
    ]
    assert unavailable == []


def test_real_rule_failure_still_logs_at_warning(
    postgres_db: PostgresHubDatabase, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "broken.yaml").write_text(BROKEN_RULE_YAML, encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        result = sync_bundled_rules(postgres_db, rules_dir)

    failures = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "Failed to sync rule 'broken-expression-rule'" in record.getMessage()
    ]
    assert len(failures) == 1
    assert len(result["errors"]) == 1
