"""Tests for cli/pipelines.py -- targeting uncovered lines.

Covers: get_workflow_loader, get_project_path, _get_project_id, get_pipeline_executor,
        _try_daemon_run, parse_input, show_pipeline, run_pipeline (daemon/local/lobster paths),
        status_pipeline, approve/reject, history, import_pipeline.
Lines targeted: 23-99, 105, 195-221, 264-384, 422-498, 534-536, 628-691
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from gobby.cli.pipelines import (
    _get_project_id,
    get_project_path,
    parse_input,
    pipelines,
)
from gobby.workflows.pipeline_state import ApprovalRequired

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# =============================================================================
# Helpers
# =============================================================================


class TestHelpers:
    def test_parse_input_valid(self) -> None:
        assert parse_input("key=value") == ("key", "value")

    def test_parse_input_with_equals_in_value(self) -> None:
        assert parse_input("key=a=b") == ("key", "a=b")

    def test_parse_input_invalid(self) -> None:
        with pytest.raises(click.BadParameter, match="key=value"):
            parse_input("noequals")

    @patch("gobby.cli.pipelines.Path")
    def test_get_project_path_found(self, mock_path_cls: MagicMock) -> None:
        cwd = MagicMock()
        gobby_dir = MagicMock()
        gobby_dir.exists.return_value = True
        cwd.__truediv__ = MagicMock(return_value=gobby_dir)
        mock_path_cls.cwd.return_value = cwd
        assert get_project_path() == cwd

    @patch("gobby.cli.pipelines.Path")
    def test_get_project_path_not_found(self, mock_path_cls: MagicMock) -> None:
        cwd = MagicMock()
        gobby_dir = MagicMock()
        gobby_dir.exists.return_value = False
        cwd.__truediv__ = MagicMock(return_value=gobby_dir)
        mock_path_cls.cwd.return_value = cwd
        assert get_project_path() is None

    @patch("gobby.cli.pipelines.get_project_path", return_value=None)
    def test_get_project_id_no_project(self, mock_pp: MagicMock) -> None:
        assert _get_project_id() == ""

    def test_get_project_id_with_project(self, tmp_path: Path) -> None:
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        (gobby_dir / "project.json").write_text('{"id": "proj-123"}')
        with patch("gobby.cli.pipelines.get_project_path", return_value=tmp_path):
            assert _get_project_id() == "proj-123"

    def test_get_project_id_no_json(self, tmp_path: Path) -> None:
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        with patch("gobby.cli.pipelines.get_project_path", return_value=tmp_path):
            assert _get_project_id() == ""

    def test_get_project_id_bad_json(self, tmp_path: Path) -> None:
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        (gobby_dir / "project.json").write_text("not json")
        with patch("gobby.cli.pipelines.get_project_path", return_value=tmp_path):
            assert _get_project_id() == ""


# =============================================================================
# show_pipeline
# =============================================================================


class TestShowPipeline:
    @patch("gobby.cli.pipelines.get_project_path", return_value=None)
    @patch("gobby.cli.pipelines.get_workflow_loader")
    def test_show_not_found(
        self, mock_loader: MagicMock, mock_pp: MagicMock, runner: CliRunner
    ) -> None:
        mock_loader.return_value.load_pipeline_sync.return_value = None
        result = runner.invoke(pipelines, ["show", "missing"])
        assert result.exit_code == 1

    @patch("gobby.cli.pipelines.get_project_path", return_value=None)
    @patch("gobby.cli.pipelines.get_workflow_loader")
    def test_show_with_inputs_outputs(
        self, mock_loader: MagicMock, mock_pp: MagicMock, runner: CliRunner
    ) -> None:
        pipeline = MagicMock()
        pipeline.name = "deploy"
        pipeline.description = "Deploy app"
        pipeline.inputs = {
            "env": {"required": True, "description": "Target environment"},
            "tag": {"required": False},
        }
        pipeline.outputs = {"url": "steps.deploy.stdout"}
        step1 = MagicMock()
        step1.id = "build"
        step1.exec = "npm run build"
        step1.prompt = None
        step1.invoke_pipeline = None
        step1.condition = None
        step2 = MagicMock()
        step2.id = "deploy"
        step2.exec = None
        step2.prompt = "Deploy the application to {{env}}"
        step2.invoke_pipeline = None
        step2.condition = "steps.build.exit_code == 0"
        step3 = MagicMock()
        step3.id = "notify"
        step3.exec = None
        step3.prompt = None
        step3.invoke_pipeline = "slack-notify"
        step3.condition = None
        pipeline.steps = [step1, step2, step3]
        mock_loader.return_value.load_pipeline_sync.return_value = pipeline

        result = runner.invoke(pipelines, ["show", "deploy"])
        assert result.exit_code == 0
        assert "Pipeline: deploy" in result.output
        assert "env (required)" in result.output
        assert "Target environment" in result.output
        assert "build (exec)" in result.output
        assert "deploy (prompt)" in result.output
        assert "notify (pipeline)" in result.output
        assert "invoke: slack-notify" in result.output
        assert "condition:" in result.output
        assert "url:" in result.output

    @patch("gobby.cli.pipelines.get_project_path", return_value=None)
    @patch("gobby.cli.pipelines.get_workflow_loader")
    def test_show_json(self, mock_loader: MagicMock, mock_pp: MagicMock, runner: CliRunner) -> None:
        pipeline = MagicMock()
        pipeline.name = "test"
        pipeline.description = "Test"
        step = MagicMock()
        step.id = "s1"
        step.exec = "echo hi"
        step.prompt = None
        step.invoke_pipeline = None
        step.condition = None
        pipeline.steps = [step]
        pipeline.inputs = {}
        pipeline.outputs = {}
        mock_loader.return_value.load_pipeline_sync.return_value = pipeline

        result = runner.invoke(pipelines, ["show", "test", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "test"


# =============================================================================
# run_pipeline - various paths
# =============================================================================


class TestRunPipeline:
    @patch("gobby.cli.pipelines._get_project_id", return_value="proj-1")
    @patch("gobby.cli.pipelines._try_daemon_run")
    @patch("gobby.cli.pipelines.get_project_path", return_value=Path("/proj"))
    @patch("gobby.cli.pipelines.get_workflow_loader")
    def test_run_daemon_waiting_approval(
        self,
        mock_loader: MagicMock,
        mock_pp: MagicMock,
        mock_daemon: MagicMock,
        mock_proj_id: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_loader.return_value.load_pipeline_sync.return_value = MagicMock()
        mock_daemon.return_value = {
            "status": "waiting_approval",
            "execution_id": "ex-1",
            "step_id": "approve-step",
            "message": "Needs approval",
            "token": "tok-abc",
        }
        result = runner.invoke(pipelines, ["run", "deploy"])
        assert result.exit_code == 0
        assert "waiting for approval" in result.output
        assert "gobby pipelines approve tok-abc" in result.output

    @patch("gobby.cli.pipelines._get_project_id", return_value="")
    @patch("gobby.cli.pipelines._try_daemon_run")
    @patch("gobby.cli.pipelines.get_project_path", return_value=Path("/proj"))
    @patch("gobby.cli.pipelines.get_workflow_loader")
    def test_run_daemon_waiting_approval_json(
        self,
        mock_loader: MagicMock,
        mock_pp: MagicMock,
        mock_daemon: MagicMock,
        mock_proj_id: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_loader.return_value.load_pipeline_sync.return_value = MagicMock()
        mock_daemon.return_value = {"status": "waiting_approval", "token": "tok"}
        result = runner.invoke(pipelines, ["run", "deploy", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "waiting_approval"

    def test_run_no_name_no_lobster(self, runner: CliRunner) -> None:
        result = runner.invoke(pipelines, ["run"])
        assert result.exit_code == 1

    @patch("gobby.cli.pipelines._get_project_id", return_value="")
    @patch("gobby.cli.pipelines.get_pipeline_executor")
    @patch("gobby.cli.pipelines.LobsterImporter")
    def test_run_lobster_file(
        self,
        mock_importer_cls: MagicMock,
        mock_executor_fn: MagicMock,
        mock_proj_id: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        lobster_file = tmp_path / "test.lobster"
        lobster_file.write_text("pipeline test")

        pipeline = MagicMock()
        pipeline.name = "test"
        mock_importer_cls.return_value.import_file.return_value = pipeline

        execution = MagicMock()
        execution.id = "ex-1"
        execution.status = MagicMock()
        execution.status.value = "completed"
        execution.pipeline_name = "test"
        execution.outputs_json = None
        mock_executor_fn.return_value.execute = MagicMock(return_value=execution)

        # Mock asyncio.run to just call the coroutine
        with patch("gobby.cli.pipelines.asyncio.run", return_value=execution):
            result = runner.invoke(pipelines, ["run", "--lobster", str(lobster_file)])

        assert result.exit_code == 0
        assert "completed" in result.output

    @patch("gobby.cli.pipelines.LobsterImporter")
    def test_run_lobster_not_found(
        self, mock_importer_cls: MagicMock, runner: CliRunner, tmp_path: Path
    ) -> None:
        lobster_file = tmp_path / "missing.lobster"
        lobster_file.write_text("")  # must exist for click.Path(exists=True)
        mock_importer_cls.return_value.import_file.side_effect = FileNotFoundError("not found")
        result = runner.invoke(pipelines, ["run", "--lobster", str(lobster_file)])
        assert result.exit_code == 1

    @patch("gobby.cli.pipelines._get_project_id", return_value="")
    @patch("gobby.cli.pipelines._try_daemon_run", return_value=None)
    @patch("gobby.cli.pipelines.get_project_path", return_value=Path("/proj"))
    @patch("gobby.cli.pipelines.get_workflow_loader")
    def test_run_local_approval_required(
        self,
        mock_loader: MagicMock,
        mock_pp: MagicMock,
        mock_daemon: MagicMock,
        mock_proj_id: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_loader.return_value.load_pipeline_sync.return_value = MagicMock()
        exc = ApprovalRequired(
            execution_id="ex-1",
            step_id="approve",
            token="tok-123",
            message="Please approve",
        )
        with patch("gobby.cli.pipelines.asyncio.run", side_effect=exc):
            result = runner.invoke(pipelines, ["run", "deploy"])
        assert result.exit_code == 0
        assert "waiting for approval" in result.output
        assert "tok-123" in result.output

    @patch("gobby.cli.pipelines._get_project_id", return_value="")
    @patch("gobby.cli.pipelines._try_daemon_run", return_value=None)
    @patch("gobby.cli.pipelines.get_project_path", return_value=Path("/proj"))
    @patch("gobby.cli.pipelines.get_workflow_loader")
    def test_run_local_generic_error(
        self,
        mock_loader: MagicMock,
        mock_pp: MagicMock,
        mock_daemon: MagicMock,
        mock_proj_id: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_loader.return_value.load_pipeline_sync.return_value = MagicMock()
        with patch("gobby.cli.pipelines.asyncio.run", side_effect=RuntimeError("boom")):
            result = runner.invoke(pipelines, ["run", "deploy"])
        assert result.exit_code == 1
        assert "Pipeline execution failed" in result.output

    @patch("gobby.cli.pipelines._get_project_id", return_value="")
    @patch("gobby.cli.pipelines._try_daemon_run", return_value=None)
    @patch("gobby.cli.pipelines.get_project_path", return_value=Path("/proj"))
    @patch("gobby.cli.pipelines.get_workflow_loader")
    def test_run_local_json_output(
        self,
        mock_loader: MagicMock,
        mock_pp: MagicMock,
        mock_daemon: MagicMock,
        mock_proj_id: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_loader.return_value.load_pipeline_sync.return_value = MagicMock()
        execution = MagicMock()
        execution.id = "ex-1"
        execution.status = MagicMock()
        execution.status.value = "completed"
        execution.pipeline_name = "test"
        execution.outputs_json = '{"url": "https://example.com"}'

        with patch("gobby.cli.pipelines.asyncio.run", return_value=execution):
            result = runner.invoke(pipelines, ["run", "deploy", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["outputs"]["url"] == "https://example.com"

    @patch("gobby.cli.pipelines._get_project_id", return_value="")
    @patch("gobby.cli.pipelines._try_daemon_run", return_value=None)
    @patch("gobby.cli.pipelines.get_project_path", return_value=Path("/proj"))
    @patch("gobby.cli.pipelines.get_workflow_loader")
    def test_run_local_json_bad_outputs(
        self,
        mock_loader: MagicMock,
        mock_pp: MagicMock,
        mock_daemon: MagicMock,
        mock_proj_id: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_loader.return_value.load_pipeline_sync.return_value = MagicMock()
        execution = MagicMock()
        execution.id = "ex-1"
        execution.status = MagicMock()
        execution.status.value = "completed"
        execution.pipeline_name = "test"
        execution.outputs_json = "not json at all"

        with patch("gobby.cli.pipelines.asyncio.run", return_value=execution):
            result = runner.invoke(pipelines, ["run", "deploy", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["outputs"] == "not json at all"


# =============================================================================
# status_pipeline
# =============================================================================


class TestStatusPipeline:
    @patch("gobby.cli.pipelines.get_execution_manager")
    def test_status_not_found(self, mock_em_fn: MagicMock, runner: CliRunner) -> None:
        mock_em_fn.return_value.get_execution.return_value = None
        result = runner.invoke(pipelines, ["status", "ex-missing"])
        assert result.exit_code == 1

    @patch("gobby.cli.pipelines.get_execution_manager")
    def test_status_json(self, mock_em_fn: MagicMock, runner: CliRunner) -> None:
        em = mock_em_fn.return_value
        ex = MagicMock()
        ex.id = "ex-1"
        ex.pipeline_name = "deploy"
        ex.status = MagicMock()
        ex.status.value = "completed"
        ex.created_at = "2024-01-01"
        ex.updated_at = "2024-01-02"
        ex.inputs_json = '{"env": "prod"}'
        ex.outputs_json = '{"url": "http://example.com"}'
        em.get_execution.return_value = ex

        step = MagicMock()
        step.id = "step-1"
        step.step_id = "build"
        step.status = MagicMock()
        step.status.value = "completed"
        em.get_steps_for_execution.return_value = [step]

        result = runner.invoke(pipelines, ["status", "ex-1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["execution"]["id"] == "ex-1"
        assert data["execution"]["inputs"]["env"] == "prod"

    @patch("gobby.cli.pipelines.get_execution_manager")
    def test_status_human_readable(self, mock_em_fn: MagicMock, runner: CliRunner) -> None:
        em = mock_em_fn.return_value
        ex = MagicMock()
        ex.id = "ex-1"
        ex.pipeline_name = "deploy"
        ex.status = MagicMock()
        ex.status.value = "running"
        ex.created_at = "2024-01-01"
        ex.updated_at = "2024-01-02"
        ex.inputs_json = None
        ex.outputs_json = None
        em.get_execution.return_value = ex

        step = MagicMock()
        step.step_id = "build"
        step.status = MagicMock()
        step.status.value = "running"
        em.get_steps_for_execution.return_value = [step]

        result = runner.invoke(pipelines, ["status", "ex-1"])
        assert result.exit_code == 0
        assert "Pipeline: deploy" in result.output
        assert "build (running)" in result.output


# =============================================================================
# approve / reject
# =============================================================================


class TestApproveReject:
    @patch("gobby.cli.pipelines.get_pipeline_executor")
    def test_approve_success(self, mock_exec_fn: MagicMock, runner: CliRunner) -> None:
        execution = MagicMock()
        execution.id = "ex-1"
        execution.pipeline_name = "deploy"
        execution.status = MagicMock()
        execution.status.value = "completed"

        with patch("gobby.cli.pipelines.asyncio.run", return_value=execution):
            result = runner.invoke(pipelines, ["approve", "tok-abc"])
        assert result.exit_code == 0
        assert "Pipeline approved" in result.output

    @patch("gobby.cli.pipelines.get_pipeline_executor")
    def test_approve_invalid_token(self, mock_exec_fn: MagicMock, runner: CliRunner) -> None:
        with patch("gobby.cli.pipelines.asyncio.run", side_effect=ValueError("bad token")):
            result = runner.invoke(pipelines, ["approve", "bad"])
        assert result.exit_code == 1
        assert "Invalid token" in result.output

    @patch("gobby.cli.pipelines.get_pipeline_executor")
    def test_approve_generic_error(self, mock_exec_fn: MagicMock, runner: CliRunner) -> None:
        with patch("gobby.cli.pipelines.asyncio.run", side_effect=RuntimeError("boom")):
            result = runner.invoke(pipelines, ["approve", "tok"])
        assert result.exit_code == 1
        assert "Approval failed" in result.output

    @patch("gobby.cli.pipelines.get_pipeline_executor")
    def test_reject_success(self, mock_exec_fn: MagicMock, runner: CliRunner) -> None:
        execution = MagicMock()
        execution.id = "ex-1"
        execution.pipeline_name = "deploy"
        execution.status = MagicMock()
        execution.status.value = "rejected"

        with patch("gobby.cli.pipelines.asyncio.run", return_value=execution):
            result = runner.invoke(pipelines, ["reject", "tok-abc"])
        assert result.exit_code == 0
        assert "Pipeline rejected" in result.output

    @patch("gobby.cli.pipelines.get_pipeline_executor")
    def test_reject_invalid_token(self, mock_exec_fn: MagicMock, runner: CliRunner) -> None:
        with patch("gobby.cli.pipelines.asyncio.run", side_effect=ValueError("bad")):
            result = runner.invoke(pipelines, ["reject", "bad"])
        assert result.exit_code == 1

    @patch("gobby.cli.pipelines.get_pipeline_executor")
    def test_reject_generic_error(self, mock_exec_fn: MagicMock, runner: CliRunner) -> None:
        with patch("gobby.cli.pipelines.asyncio.run", side_effect=RuntimeError("boom")):
            result = runner.invoke(pipelines, ["reject", "tok"])
        assert result.exit_code == 1
        assert "Rejection failed" in result.output

    @patch("gobby.cli.pipelines.get_pipeline_executor")
    def test_approve_json(self, mock_exec_fn: MagicMock, runner: CliRunner) -> None:
        execution = MagicMock()
        execution.id = "ex-1"
        execution.pipeline_name = "deploy"
        execution.status = MagicMock()
        execution.status.value = "completed"

        with patch("gobby.cli.pipelines.asyncio.run", return_value=execution):
            result = runner.invoke(pipelines, ["approve", "tok", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "completed"

    @patch("gobby.cli.pipelines.get_pipeline_executor")
    def test_reject_json(self, mock_exec_fn: MagicMock, runner: CliRunner) -> None:
        execution = MagicMock()
        execution.id = "ex-1"
        execution.pipeline_name = "deploy"
        execution.status = MagicMock()
        execution.status.value = "rejected"

        with patch("gobby.cli.pipelines.asyncio.run", return_value=execution):
            result = runner.invoke(pipelines, ["reject", "tok", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "rejected"


# =============================================================================
# history
# =============================================================================


class TestHistoryPipeline:
    @patch("gobby.cli.pipelines.get_execution_manager")
    def test_history_empty(self, mock_em_fn: MagicMock, runner: CliRunner) -> None:
        mock_em_fn.return_value.list_executions.return_value = []
        result = runner.invoke(pipelines, ["history", "deploy"])
        assert result.exit_code == 0
        assert "No executions found" in result.output

    @patch("gobby.cli.pipelines.get_execution_manager")
    def test_history_with_results(self, mock_em_fn: MagicMock, runner: CliRunner) -> None:
        ex = MagicMock()
        ex.id = "ex-1"
        ex.status = MagicMock()
        ex.status.value = "completed"
        ex.created_at = "2024-01-01"
        ex.updated_at = "2024-01-02"
        mock_em_fn.return_value.list_executions.return_value = [ex]
        result = runner.invoke(pipelines, ["history", "deploy"])
        assert result.exit_code == 0
        assert "Execution history" in result.output

    @patch("gobby.cli.pipelines.get_execution_manager")
    def test_history_json(self, mock_em_fn: MagicMock, runner: CliRunner) -> None:
        ex = MagicMock()
        ex.id = "ex-1"
        ex.status = MagicMock()
        ex.status.value = "failed"
        ex.created_at = "2024-01-01"
        ex.updated_at = "2024-01-02"
        mock_em_fn.return_value.list_executions.return_value = [ex]
        result = runner.invoke(pipelines, ["history", "deploy", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1


# =============================================================================
# import
# =============================================================================


class TestImportPipeline:
    @patch("gobby.cli.pipelines.LobsterImporter")
    @patch("gobby.cli.pipelines.get_project_path", return_value=None)
    def test_import_no_project_no_output(
        self, mock_pp: MagicMock, mock_imp: MagicMock, runner: CliRunner, tmp_path: Path
    ) -> None:
        f = tmp_path / "test.lobster"
        f.write_text("pipeline test")
        result = runner.invoke(pipelines, ["import", str(f)])
        assert result.exit_code == 1

    @patch("gobby.cli.pipelines.LobsterImporter")
    @patch("gobby.cli.pipelines.get_project_path", return_value=None)
    def test_import_file_not_found(
        self, mock_pp: MagicMock, mock_imp: MagicMock, runner: CliRunner, tmp_path: Path
    ) -> None:
        f = tmp_path / "test.lobster"
        f.write_text("x")
        mock_imp.return_value.import_file.side_effect = FileNotFoundError("no")
        result = runner.invoke(
            pipelines, ["import", str(f), "--output", str(tmp_path / "out.yaml")]
        )
        assert result.exit_code == 1

    @patch("gobby.cli.pipelines.LobsterImporter")
    @patch("gobby.cli.pipelines.get_project_path", return_value=None)
    def test_import_parse_error(
        self, mock_pp: MagicMock, mock_imp: MagicMock, runner: CliRunner, tmp_path: Path
    ) -> None:
        f = tmp_path / "test.lobster"
        f.write_text("x")
        mock_imp.return_value.import_file.side_effect = RuntimeError("parse fail")
        result = runner.invoke(
            pipelines, ["import", str(f), "--output", str(tmp_path / "out.yaml")]
        )
        assert result.exit_code == 1
        assert "Failed to import" in result.output

    @patch("gobby.cli.pipelines.LobsterImporter")
    @patch("gobby.cli.pipelines.get_project_path", return_value=None)
    def test_import_to_custom_output(
        self, mock_pp: MagicMock, mock_imp: MagicMock, runner: CliRunner, tmp_path: Path
    ) -> None:
        f = tmp_path / "test.lobster"
        f.write_text("x")

        pipeline = MagicMock()
        pipeline.name = "my-pipeline"
        pipeline.description = "desc"
        pipeline.version = "1.0"
        pipeline.inputs = {}
        pipeline.outputs = {}
        step = MagicMock()
        step.id = "s1"
        step.exec = "echo hi"
        step.prompt = None
        step.invoke_pipeline = None
        step.condition = None
        step.input = None
        step.approval = None
        step.tools = None
        pipeline.steps = [step]
        mock_imp.return_value.import_file.return_value = pipeline

        out = tmp_path / "out.yaml"
        result = runner.invoke(pipelines, ["import", str(f), "--output", str(out)])
        assert result.exit_code == 0
        assert "Imported 'my-pipeline'" in result.output
        assert out.exists()

    @patch("gobby.cli.pipelines.LobsterImporter")
    def test_import_to_project_dir(
        self, mock_imp: MagicMock, runner: CliRunner, tmp_path: Path
    ) -> None:
        f = tmp_path / "test.lobster"
        f.write_text("x")

        pipeline = MagicMock()
        pipeline.name = "test-pipeline"
        pipeline.description = None
        pipeline.version = "1.0"
        pipeline.inputs = {"key": {"required": True}}
        pipeline.outputs = {"out": "steps.s1.stdout"}
        step = MagicMock()
        step.id = "s1"
        step.exec = "echo"
        step.prompt = None
        step.invoke_pipeline = None
        step.condition = None
        step.input = {"key": "val"}
        step.approval = MagicMock()
        step.approval.required = True
        step.approval.message = "Approve?"
        step.approval.timeout_seconds = 300
        step.tools = ["read_file"]
        pipeline.steps = [step]
        mock_imp.return_value.import_file.return_value = pipeline

        with patch("gobby.cli.pipelines.get_project_path", return_value=tmp_path):
            result = runner.invoke(pipelines, ["import", str(f)])
        assert result.exit_code == 0
        assert "Imported 'test-pipeline'" in result.output
        dest = tmp_path / ".gobby" / "workflows" / "test-pipeline.yaml"
        assert dest.exists()
