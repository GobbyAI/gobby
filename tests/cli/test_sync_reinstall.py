"""Reinstall must stay atomic and abort when integrity skip overlaps."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.sync import _reinstall_bundled_definitions, sync
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.sync.integrity import IntegrityResult

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def no_running_daemon() -> Iterator[None]:
    """Default to "no daemon answered" so the checkout gate stays out of the way."""
    with patch("gobby.cli.sync._running_daemon_install_dir", return_value=None):
        yield


@patch("gobby.sync_registry.sync_bundled_content_to_db")
@patch("gobby.cli.runtime.require_cli_database")
@patch("gobby.sync.integrity.verify_bundled_integrity")
@patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
@patch("gobby.utils.dev.is_dev_mode", return_value=False)
def test_reinstall_aborts_when_selected_types_overlap_skip_types(
    _dev: MagicMock,
    _install: MagicMock,
    mock_verify: MagicMock,
    mock_load: MagicMock,
    mock_sync: MagicMock,
    runner: CliRunner,
) -> None:
    mock_verify.return_value = IntegrityResult(
        dirty_files=["shared/workflows/rules/demo.yaml"],
        git_available=True,
        checked=True,
        source="git",
    )
    mock_load.return_value = MagicMock()

    with patch("gobby.cli.sync._delete_installed_definitions") as mock_delete:
        result = runner.invoke(sync, ["--reinstall", "rules"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "Cannot reinstall types blocked by integrity check: rules" in result.output
    mock_delete.assert_not_called()
    mock_sync.assert_not_called()


@patch("gobby.sync_registry.sync_bundled_content_to_db")
@patch("gobby.cli.runtime.require_cli_database")
@patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
@patch("gobby.utils.dev.is_dev_mode", return_value=False)
def test_reinstall_force_skips_overlap_guard(
    _dev: MagicMock,
    _install: MagicMock,
    mock_load: MagicMock,
    mock_sync: MagicMock,
    runner: CliRunner,
) -> None:
    mock_load.return_value = MagicMock()
    mock_sync.return_value = {"total_synced": 1, "errors": [], "details": {"rules": {}}}

    with patch("gobby.cli.sync._delete_installed_definitions", return_value=1) as mock_delete:
        result = runner.invoke(sync, ["--reinstall", "rules", "--force"], catch_exceptions=False)

    assert result.exit_code == 0
    mock_delete.assert_called_once()
    mock_sync.assert_called_once()
    assert mock_sync.call_args.kwargs.get("only") == {"rules"}


def test_failed_reinstall_leaves_prior_bundled_rows(hub_db: HubDatabase) -> None:
    manager = RuleDefinitionManager(hub_db)
    prior = manager.create(
        name="keep-prior-rule",
        definition_json={"event": "before_tool", "action": "allow"},
        source="installed",
        tags=["gobby"],
    )

    def fail_sync(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {"total_synced": 0, "errors": ["bundle invalid"], "details": {}}

    with patch("gobby.sync_registry.sync_bundled_content_to_db", side_effect=fail_sync):
        deleted, result = _reinstall_bundled_definitions(hub_db, {"rules"}, skip_types=None)

    assert deleted == 0
    assert result["errors"]
    kept = manager.get(prior.id)
    assert kept.name == "keep-prior-rule"
    assert kept.deleted_at is None


@patch("gobby.sync_registry.sync_bundled_content_to_db")
@patch("gobby.cli.runtime.require_cli_database")
@patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
@patch("gobby.utils.dev.is_dev_mode", return_value=False)
def test_reinstall_disposition_failure_raises_click_and_keeps_rows(
    _dev: MagicMock,
    _install: MagicMock,
    mock_load: MagicMock,
    mock_sync: MagicMock,
    runner: CliRunner,
    hub_db: HubDatabase,
) -> None:
    manager = RuleDefinitionManager(hub_db)
    prior = manager.create(
        name="keep-disposition-rule",
        definition_json={"event": "before_tool", "effects": [{"type": "block", "reason": "x"}]},
        source="installed",
        tags=["gobby"],
    )
    mock_load.return_value = hub_db
    diagnostic = (
        "delivery disposition: Rule 'maybe' effect 1 (set_variable 'g'): "
        "ambiguous delivery suppressor"
    )
    mock_sync.return_value = {"total_synced": 0, "errors": [diagnostic], "details": {}}

    result = runner.invoke(sync, ["--reinstall", "rules", "--force"])

    assert result.exit_code == 1
    assert "maybe" in result.output
    assert "Warning:" not in result.output
    kept = manager.get(prior.id)
    assert kept.name == "keep-disposition-rule"
    assert kept.deleted_at is None


@patch("gobby.sync_registry.sync_bundled_content_to_db")
@patch("gobby.cli.runtime.require_cli_database")
@patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
@patch("gobby.utils.dev.is_dev_mode", return_value=False)
def test_reinstall_partial_disposition_failure_raises_click(
    _dev: MagicMock,
    _install: MagicMock,
    mock_load: MagicMock,
    mock_sync: MagicMock,
    runner: CliRunner,
) -> None:
    mock_load.return_value = MagicMock()
    mock_sync.return_value = {
        "total_synced": 0,
        "errors": ["delivery disposition: partial failure: injected write failure"],
        "details": {},
    }

    result = runner.invoke(sync, ["--reinstall", "rules", "--force"])

    assert result.exit_code == 1
    assert "partial failure" in result.output
    assert "Warning:" not in result.output
