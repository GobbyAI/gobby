"""Focused CLI coverage for plan 6.1: domain replacements of gobby workflows."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.workflows.dry_run import EvaluationItem, WorkflowEvaluation
from gobby.workflows.imports import sync_imported_workflows

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOWS_PACKAGE = _REPO / "src" / "gobby" / "cli" / "workflows"
_DELETED_WORKFLOW_SUITES = (
    _REPO / "tests" / "cli" / "test_cli_workflows.py",
    _REPO / "tests" / "cli" / "test_workflows.py",
    _REPO / "tests" / "cli" / "test_workflows_coverage.py",
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_workflows_group_is_gone_from_root_cli(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "workflows" not in cli.commands
    assert "  workflows " not in result.output
    result_missing = runner.invoke(cli, ["workflows", "list"])
    assert result_missing.exit_code != 0
    assert "No such command" in result_missing.output


def test_domain_replacements_are_registered(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "variables" in cli.commands
    assert "  variables " in result.output

    agents_help = runner.invoke(cli, ["agents", "--help"])
    assert agents_help.exit_code == 0
    assert "  steps " in agents_help.output
    assert "  check " in agents_help.output

    pipelines_help = runner.invoke(cli, ["pipelines", "--help"])
    assert pipelines_help.exit_code == 0
    assert "  show " in pipelines_help.output
    assert "  check " in pipelines_help.output


def test_agent_steps_module_exists_under_line_cap() -> None:
    steps_path = _REPO / "src" / "gobby" / "cli" / "agents_steps.py"
    agents_path = _REPO / "src" / "gobby" / "cli" / "agents.py"
    assert steps_path.is_file()
    assert len(steps_path.read_text().splitlines()) < 1000
    assert len(agents_path.read_text().splitlines()) < 1000


def test_no_production_module_imports_cli_workflows() -> None:
    src_root = _REPO / "src" / "gobby"
    offenders: list[str] = []
    for path in src_root.rglob("*.py"):
        if "cli/workflows" in path.as_posix():
            offenders.append(str(path.relative_to(_REPO)))
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "gobby.cli.workflows" or node.module.startswith(
                    "gobby.cli.workflows."
                ):
                    offenders.append(str(path.relative_to(_REPO)))
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "gobby.cli.workflows" or alias.name.startswith(
                        "gobby.cli.workflows."
                    ):
                        offenders.append(str(path.relative_to(_REPO)))
    assert offenders == []
    assert not _WORKFLOWS_PACKAGE.exists()


def test_deleted_workflows_suites_and_rehomed_pipeline_patches() -> None:
    for path in _DELETED_WORKFLOW_SUITES:
        assert not path.exists(), path
    coverage = (_REPO / "tests" / "cli" / "test_pipelines_coverage.py").read_text()
    assert "gobby.cli.workflows.common.Path" not in coverage
    assert '@patch("gobby.cli.pipelines.Path")' in coverage


def test_agents_steps_reads_agent_step_instance(runner: CliRunner) -> None:
    instance = MagicMock()
    instance.agent_name = "analyst"
    instance.current_step = "research"
    instance.snapshot.steps = [MagicMock(name="research"), MagicMock(name="write")]
    instance.snapshot.steps[0].name = "research"
    instance.snapshot.steps[1].name = "write"
    instance.snapshot.exit_condition = "done"
    instance.variables = {"topic": "storage"}

    with (
        patch("gobby.cli.agents_steps.resolve_session_id", return_value="sess-1"),
        patch("gobby.cli.agents_steps.require_cli_database", return_value=MagicMock()),
        patch("gobby.cli.agents_steps.AgentStepInstanceManager") as manager_cls,
    ):
        manager_cls.return_value.get_for_session.return_value = instance
        result = runner.invoke(cli, ["agents", "steps", "--session", "sess-1"])

    assert result.exit_code == 0
    assert "analyst" in result.output
    assert "research" in result.output
    manager_cls.return_value.get_for_session.assert_called_once_with("sess-1")


def test_agents_check_wraps_evaluate_agent_definition(runner: CliRunner) -> None:
    evaluation = WorkflowEvaluation(valid=True, workflow_name="analyst")
    evaluation.items.append(
        EvaluationItem(layer="structure", level="info", code="OK", message="ok")
    )
    agent = MagicMock()
    agent.name = "analyst"

    with (
        patch("gobby.cli.agents_steps.require_cli_database", return_value=MagicMock()),
        patch("gobby.cli.agents_steps.resolve_agent", return_value=agent),
        patch(
            "gobby.cli.agents_steps.evaluate_agent_definition",
            new_callable=AsyncMock,
            return_value=evaluation,
        ) as mock_eval,
    ):
        result = runner.invoke(cli, ["agents", "check", "analyst"])

    assert result.exit_code == 0
    assert "VALID" in result.output
    mock_eval.assert_awaited()


def test_pipelines_check_wraps_evaluate_pipeline_definition(runner: CliRunner) -> None:
    evaluation = WorkflowEvaluation(valid=True, workflow_name="deploy")
    evaluation.items.append(
        EvaluationItem(
            layer="structure",
            level="info",
            code="PIPELINE_TYPE",
            message="'deploy' is a pipeline workflow — step checks skipped",
        )
    )

    with (
        patch("gobby.cli.pipelines.get_workflow_loader", return_value=MagicMock()),
        patch("gobby.cli.pipelines._get_project_id", return_value="proj-1"),
        patch(
            "gobby.cli.pipelines_catalog.evaluate_pipeline_definition",
            new_callable=AsyncMock,
            return_value=evaluation,
        ) as mock_eval,
    ):
        result = runner.invoke(cli, ["pipelines", "check", "deploy"])

    assert result.exit_code == 0
    assert "VALID" in result.output
    mock_eval.assert_awaited()


def test_variables_get_and_set_use_session_scope_model(runner: CliRunner) -> None:
    manager = MagicMock()
    manager.get_variables.return_value = {
        "default_only": True,
        "session_epic": "#47",
    }

    with (
        patch("gobby.cli.variables.get_session_var_manager", return_value=manager),
        patch("gobby.cli.variables.resolve_session_id", return_value="sess-1"),
        patch("gobby.cli.variables.close_session_var_manager") as close_manager,
    ):
        get_one = runner.invoke(cli, ["variables", "get", "session_epic", "--session", "sess-1"])
        get_all = runner.invoke(cli, ["variables", "get", "--session", "sess-1", "--json"])
        set_var = runner.invoke(
            cli, ["variables", "set", "is_worktree", "true", "--session", "sess-1"]
        )

    assert get_one.exit_code == 0
    assert "session_epic" in get_one.output
    assert get_all.exit_code == 0
    payload = json.loads(get_all.output)
    assert payload["variables"]["default_only"] is True
    assert payload["variables"]["session_epic"] == "#47"
    assert set_var.exit_code == 0
    manager.set_variable.assert_called_once_with("sess-1", "is_worktree", True)
    assert close_manager.call_count >= 3


def test_sync_reinstall_uses_registry_without_legacy_sql(runner: CliRunner) -> None:
    sync_source = (_REPO / "src" / "gobby" / "cli" / "sync.py").read_text()
    assert "workflow_definitions" not in sync_source
    assert "--reinstall" in sync_source

    with (
        patch("gobby.utils.dev.is_dev_mode", return_value=True),
        patch("gobby.cli.runtime.require_cli_database", return_value=MagicMock()),
        patch("gobby.cli.sync._delete_installed_definitions", return_value=2) as mock_delete,
        patch(
            "gobby.sync_registry.sync_bundled_content_to_db",
            return_value={"total_synced": 4, "errors": [], "details": {}},
        ) as mock_sync,
    ):
        result = runner.invoke(cli, ["sync", "--reinstall", "rules", "--force"])

    assert result.exit_code == 0
    mock_delete.assert_called_once()
    mock_sync.assert_called_once()
    assert mock_sync.call_args.kwargs.get("only") == {"rules"}


def test_sync_imported_workflows_reads_kind_subdirs_not_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_dir = tmp_path / "global-workflows"
    for kind in ("rules", "agents", "pipelines", "variables"):
        (global_dir / kind).mkdir(parents=True)
    (global_dir / "root-only.yaml").write_text(
        "name: root-only\ntype: pipeline\nsteps:\n  - id: s\n    exec: echo\n",
        encoding="utf-8",
    )
    (global_dir / "pipelines" / "kind-pipe.yaml").write_text(
        "name: kind-pipe\ntype: pipeline\nsteps:\n  - id: s\n    exec: echo\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("gobby.workflows.imports.get_global_workflows_dir", lambda: global_dir)
    seen: list[str] = []

    def _capture(_db: object, path: Path, _project_id: str | None) -> None:
        seen.append(path.name)

    monkeypatch.setattr("gobby.workflows.imports.sync_imported_workflow_file", _capture)
    with patch("gobby.workflows.imports.LocalProjectManager") as project_manager:
        project_manager.return_value.list.return_value = []
        result = sync_imported_workflows(MagicMock())

    assert result["synced"] == 1
    assert "kind-pipe.yaml" in seen
    assert "root-only.yaml" not in seen
