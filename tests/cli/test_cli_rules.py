"""Tests for gobby rules CLI commands."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


@pytest.fixture
def cli_runner():
    return CliRunner()


@pytest.fixture
def mock_manager():
    """Mock RuleDefinitionManager."""
    return MagicMock()


def _make_rule_row(
    name: str = "test-rule",
    enabled: bool = True,
    priority: int = 50,
    source: str = "template",
    description: str | None = "A test rule",
    tags: list[str] | None = None,
    definition_json: str | None = None,
    workflow_type: str = "rule",
):
    """Create a mock RuleDefinitionRow for rules."""
    row = MagicMock()
    row.id = f"id-{name}"
    row.name = name
    row.enabled = enabled
    row.priority = priority
    row.source = source
    row.description = description
    row.tags = tags or []
    row.workflow_type = workflow_type
    row.definition_json = definition_json or json.dumps(
        {
            "event": "before_tool",
            "group": "test-group",
            "effects": [{"type": "block", "tools": ["Bash"], "reason": "test"}],
            "when": "not task_claimed",
        }
    )
    row.deleted_at = None
    return row


def _make_http_response(
    status_code: int = 200,
    payload: dict[str, object] | None = None,
    text: str = "",
) -> MagicMock:
    """Create a mock daemon HTTP response."""
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    if payload is None:
        response.json.side_effect = ValueError("no json")
    else:
        response.json.return_value = payload
    return response


# ==============================================================================
# Tests for list command
# ==============================================================================


class TestListRules:
    def test_list_empty(self, cli_runner, mock_manager) -> None:
        from gobby.cli.rules import rules

        mock_manager.list_all.return_value = []

        with patch("gobby.cli.rules._get_manager", return_value=mock_manager):
            result = cli_runner.invoke(rules, ["list"])
            assert result.exit_code == 0
            assert "No rules found" in result.output

    def test_list_with_rules(self, cli_runner, mock_manager) -> None:
        from gobby.cli.rules import rules

        mock_manager.list_all.return_value = [
            _make_rule_row("rule-a", enabled=True),
            _make_rule_row("rule-b", enabled=False),
        ]

        with patch("gobby.cli.rules._get_manager", return_value=mock_manager):
            result = cli_runner.invoke(rules, ["list"])
            assert result.exit_code == 0
            assert "rule-a" in result.output
            assert "rule-b" in result.output

    def test_list_filter_by_event(self, cli_runner, mock_manager) -> None:
        from gobby.cli.rules import rules

        mock_manager.list_by_event.return_value = [
            _make_rule_row("event-rule"),
        ]

        with patch("gobby.cli.rules._get_manager", return_value=mock_manager):
            result = cli_runner.invoke(rules, ["list", "--event", "before_tool"])
            assert result.exit_code == 0
            assert "event-rule" in result.output
            mock_manager.list_by_event.assert_called_once_with("before_tool", enabled=None)

    def test_list_filter_by_group(self, cli_runner, mock_manager) -> None:
        from gobby.cli.rules import rules

        mock_manager.list_by_group.return_value = [
            _make_rule_row("group-rule"),
        ]

        with patch("gobby.cli.rules._get_manager", return_value=mock_manager):
            result = cli_runner.invoke(rules, ["list", "--group", "worker-safety"])
            assert result.exit_code == 0
            assert "group-rule" in result.output
            mock_manager.list_by_group.assert_called_once_with("worker-safety", enabled=None)

    def test_list_filter_enabled(self, cli_runner, mock_manager) -> None:
        from gobby.cli.rules import rules

        mock_manager.list_all.return_value = [_make_rule_row("enabled-rule")]

        with patch("gobby.cli.rules._get_manager", return_value=mock_manager):
            result = cli_runner.invoke(rules, ["list", "--enabled"])
            assert result.exit_code == 0
            mock_manager.list_all.assert_called_once_with(enabled=True)

    def test_list_filter_disabled(self, cli_runner, mock_manager) -> None:
        from gobby.cli.rules import rules

        mock_manager.list_all.return_value = [_make_rule_row("disabled-rule", enabled=False)]

        with patch("gobby.cli.rules._get_manager", return_value=mock_manager):
            result = cli_runner.invoke(rules, ["list", "--disabled"])
            assert result.exit_code == 0
            mock_manager.list_all.assert_called_once_with(enabled=False)

    def test_list_json(self, cli_runner, mock_manager) -> None:
        from gobby.cli.rules import rules

        mock_manager.list_all.return_value = [
            _make_rule_row("json-rule"),
        ]

        with patch("gobby.cli.rules._get_manager", return_value=mock_manager):
            result = cli_runner.invoke(rules, ["list", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "rules" in data
            assert len(data["rules"]) == 1
            assert data["rules"][0]["name"] == "json-rule"


# ==============================================================================
# Tests for show command
# ==============================================================================


class TestShowRule:
    def test_show_found(self, cli_runner, mock_manager) -> None:
        from gobby.cli.rules import rules

        mock_manager.get_by_name.return_value = _make_rule_row("my-rule")

        with patch("gobby.cli.rules._get_manager", return_value=mock_manager):
            result = cli_runner.invoke(rules, ["show", "my-rule"])
            assert result.exit_code == 0
            assert "my-rule" in result.output
            assert "before_tool" in result.output

    def test_show_not_found(self, cli_runner, mock_manager) -> None:
        from gobby.cli.rules import rules

        mock_manager.get_by_name.return_value = None

        with patch("gobby.cli.rules._get_manager", return_value=mock_manager):
            result = cli_runner.invoke(rules, ["show", "missing"])
            assert result.exit_code == 1
            assert "not found" in result.output

    def test_show_json(self, cli_runner, mock_manager) -> None:
        from gobby.cli.rules import rules

        mock_manager.get_by_name.return_value = _make_rule_row("json-rule")

        with patch("gobby.cli.rules._get_manager", return_value=mock_manager):
            result = cli_runner.invoke(rules, ["show", "json-rule", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["name"] == "json-rule"
            assert "effects" in data


# ==============================================================================
# Tests for enable command
# ==============================================================================


class TestEnableRule:
    def test_enable(self, cli_runner) -> None:
        from gobby.cli.rules import rules

        mock_client = MagicMock()
        mock_client.call_http_api.return_value = _make_http_response()

        with patch("gobby.cli.rules._get_daemon_client", return_value=mock_client):
            result = cli_runner.invoke(rules, ["enable", "my-rule"])
            assert result.exit_code == 0
            assert "Enabled" in result.output
            mock_client.call_http_api.assert_called_once_with(
                "/api/rules/my-rule/toggle",
                method="PUT",
                json_data={"enabled": True},
            )

    def test_enable_quotes_rule_name(self, cli_runner) -> None:
        from gobby.cli.rules import rules

        mock_client = MagicMock()
        mock_client.call_http_api.return_value = _make_http_response()

        with patch("gobby.cli.rules._get_daemon_client", return_value=mock_client):
            result = cli_runner.invoke(rules, ["enable", "group/my rule"])
            assert result.exit_code == 0
            mock_client.call_http_api.assert_called_once_with(
                "/api/rules/group%2Fmy%20rule/toggle",
                method="PUT",
                json_data={"enabled": True},
            )

    def test_enable_not_found(self, cli_runner) -> None:
        from gobby.cli.rules import rules

        mock_client = MagicMock()
        mock_client.call_http_api.return_value = _make_http_response(
            status_code=404,
            payload={"detail": "Rule 'missing' not found"},
        )

        with patch("gobby.cli.rules._get_daemon_client", return_value=mock_client):
            result = cli_runner.invoke(rules, ["enable", "missing"])
            assert result.exit_code == 1
            assert "Rule not found: missing" in result.output

    def test_enable_daemon_error(self, cli_runner) -> None:
        from gobby.cli.rules import rules

        mock_client = MagicMock()
        mock_client.call_http_api.return_value = _make_http_response(
            status_code=500,
            payload={"detail": "Internal server error"},
        )

        with patch("gobby.cli.rules._get_daemon_client", return_value=mock_client):
            result = cli_runner.invoke(rules, ["enable", "my-rule"])
            assert result.exit_code == 1
            assert "HTTP 500: Internal server error" in result.output


# ==============================================================================
# Tests for disable command
# ==============================================================================


class TestDisableRule:
    def test_disable(self, cli_runner) -> None:
        from gobby.cli.rules import rules

        mock_client = MagicMock()
        mock_client.call_http_api.return_value = _make_http_response()

        with patch("gobby.cli.rules._get_daemon_client", return_value=mock_client):
            result = cli_runner.invoke(rules, ["disable", "my-rule"])
            assert result.exit_code == 0
            assert "Disabled" in result.output
            mock_client.call_http_api.assert_called_once_with(
                "/api/rules/my-rule/toggle",
                method="PUT",
                json_data={"enabled": False},
            )

    def test_disable_not_found(self, cli_runner) -> None:
        from gobby.cli.rules import rules

        mock_client = MagicMock()
        mock_client.call_http_api.return_value = _make_http_response(status_code=404)

        with patch("gobby.cli.rules._get_daemon_client", return_value=mock_client):
            result = cli_runner.invoke(rules, ["disable", "missing"])
            assert result.exit_code == 1
            assert "Rule not found: missing" in result.output


# ==============================================================================
# Tests for import command
# ==============================================================================


class TestImportRules:
    def test_import_file(self, cli_runner, tmp_path) -> None:
        from gobby.cli.rules import rules

        rule_file = tmp_path / "test-rules.yaml"
        rule_file.write_text(
            "group: test\nrules:\n  my-rule:\n    event: before_tool\n    effect:\n      type: block\n      reason: test\n"
        )

        mock_db = MagicMock()

        with (
            patch("gobby.cli.rules.require_cli_database", return_value=mock_db),
            patch(
                "gobby.cli.installers.shared.registered_project_id", return_value="project-1"
            ) as mock_scope,
            patch("gobby.workflows.sync_rules.sync_rule_file") as mock_sync,
        ):
            mock_sync.return_value = {"success": True, "synced": 1, "updated": 0, "errors": []}
            result = cli_runner.invoke(rules, ["import", str(rule_file)])
            assert result.exit_code == 0
            assert "Imported rules: 1 new, 0 updated (project project-1)" in result.output
            mock_scope.assert_called_once_with(mock_db, Path.cwd())
            mock_sync.assert_called_once_with(mock_db, rule_file=rule_file, project_id="project-1")

    def test_import_file_not_found(self, cli_runner) -> None:
        from gobby.cli.rules import rules

        result = cli_runner.invoke(rules, ["import", "/nonexistent.yaml"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_import_one_shot_persists_on_receipt(
        self, cli_runner: CliRunner, tmp_path: Path, temp_db: HubDatabase
    ) -> None:
        from gobby.cli.rules import rules
        from gobby.storage.definitions.rules import RuleDefinitionManager

        rule_file = tmp_path / "once.yaml"
        rule_file.write_text(
            "rules:\n"
            "  once:\n"
            "    event: turn_start\n"
            "    when: not variables.get('shown')\n"
            "    effects:\n"
            "      - type: inject_context\n"
            "        template: hello\n"
            "      - type: set_variable\n"
            "        variable: shown\n"
            "        value: true\n",
            encoding="utf-8",
        )

        with (
            patch("gobby.cli.rules.require_cli_database", return_value=temp_db),
            patch("gobby.cli.installers.shared.registered_project_id", return_value=None),
        ):
            result = cli_runner.invoke(rules, ["import", str(rule_file)])

        assert result.exit_code == 0
        row = RuleDefinitionManager(temp_db).get_by_name("once")
        assert row is not None
        assert [effect.get("delivery", "eager") for effect in row.definition_json["effects"]] == [
            "on_receipt",
            "on_receipt",
        ]

    def test_import_not_yaml(self, cli_runner, tmp_path) -> None:
        from gobby.cli.rules import rules

        bad_file = tmp_path / "rules.txt"
        bad_file.write_text("not yaml format")

        result = cli_runner.invoke(rules, ["import", str(bad_file)])
        assert result.exit_code == 1
        assert ".yaml" in result.output


# ==============================================================================
# Tests for export command
# ==============================================================================


class TestExportRules:
    def test_export_all(self, cli_runner, mock_manager) -> None:
        from gobby.cli.rules import rules

        mock_manager.list_all.return_value = [
            _make_rule_row("rule-a"),
            _make_rule_row("rule-b"),
        ]

        with patch("gobby.cli.rules._get_manager", return_value=mock_manager):
            result = cli_runner.invoke(rules, ["export"])
            assert result.exit_code == 0
            assert "rule-a" in result.output
            assert "rule-b" in result.output

    def test_export_by_group(self, cli_runner, mock_manager) -> None:
        from gobby.cli.rules import rules

        mock_manager.list_by_group.return_value = [
            _make_rule_row("group-rule"),
        ]

        with patch("gobby.cli.rules._get_manager", return_value=mock_manager):
            result = cli_runner.invoke(rules, ["export", "--group", "test-group"])
            assert result.exit_code == 0
            assert "group-rule" in result.output
            mock_manager.list_by_group.assert_called_once_with("test-group", enabled=None)

    def test_export_empty(self, cli_runner, mock_manager) -> None:
        from gobby.cli.rules import rules

        mock_manager.list_all.return_value = []

        with patch("gobby.cli.rules._get_manager", return_value=mock_manager):
            result = cli_runner.invoke(rules, ["export"])
            assert result.exit_code == 0
            assert "No rules" in result.output


# ==============================================================================
# Tests for audit command
# ==============================================================================


class TestAuditRules:
    def test_audit_no_entries(self, cli_runner) -> None:
        from gobby.cli.rules import rules

        with patch("gobby.cli.rules._get_audit_manager") as mock_get:
            mock_audit = MagicMock()
            mock_audit.get_entries.return_value = []
            mock_get.return_value = mock_audit

            result = cli_runner.invoke(rules, ["audit"])
            assert result.exit_code == 0
            assert "No audit entries" in result.output

    def test_audit_with_entries(self, cli_runner) -> None:
        from datetime import UTC, datetime

        from gobby.cli.rules import rules

        mock_entry = MagicMock()
        mock_entry.id = "entry-1"
        mock_entry.timestamp = datetime.now(UTC)
        mock_entry.event_type = "before_tool"
        mock_entry.tool_name = "Edit"
        mock_entry.rule_id = "no-edit-rule"
        mock_entry.result = "block"
        mock_entry.reason = "Not allowed"

        with patch("gobby.cli.rules._get_audit_manager") as mock_get:
            mock_audit = MagicMock()
            mock_audit.get_entries.return_value = [mock_entry]
            mock_get.return_value = mock_audit

            result = cli_runner.invoke(rules, ["audit"])
            assert result.exit_code == 0
            assert "BLOCK" in result.output
            assert "before_tool" in result.output

    def test_audit_with_session(self, cli_runner) -> None:
        from gobby.cli.rules import rules

        with patch("gobby.cli.rules._get_audit_manager") as mock_get:
            mock_audit = MagicMock()
            mock_audit.get_entries.return_value = []
            mock_get.return_value = mock_audit

            result = cli_runner.invoke(rules, ["audit", "--session", "sess-123"])
            assert result.exit_code == 0
            mock_audit.get_entries.assert_called_once()
            # Session should be passed through
            call_kwargs = mock_audit.get_entries.call_args
            assert call_kwargs[1].get("session_id") == "sess-123" or (
                len(call_kwargs[0]) > 0 and "sess-123" in str(call_kwargs)
            )

    def test_audit_json(self, cli_runner) -> None:
        from datetime import UTC, datetime

        from gobby.cli.rules import rules

        mock_entry = MagicMock()
        mock_entry.id = "entry-1"
        mock_entry.timestamp = datetime.now(UTC)
        mock_entry.event_type = "before_tool"
        mock_entry.tool_name = "Bash"
        mock_entry.rule_id = "test-rule"
        mock_entry.result = "allow"
        mock_entry.reason = None

        with patch("gobby.cli.rules._get_audit_manager") as mock_get:
            mock_audit = MagicMock()
            mock_audit.get_entries.return_value = [mock_entry]
            mock_get.return_value = mock_audit

            result = cli_runner.invoke(rules, ["audit", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert isinstance(data, list)
            assert len(data) == 1
