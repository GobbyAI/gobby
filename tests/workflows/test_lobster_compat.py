"""Tests for Lobster compatibility/migration utilities."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


class TestLobsterImporterConvertStep:
    """Tests for LobsterImporter.convert_step() method."""

    def test_command_maps_to_exec(self) -> None:
        """Verify Lobster 'command' field maps to Gobby 'exec' field."""
        from gobby.workflows.lobster_compat import LobsterImporter

        importer = LobsterImporter()

        lobster_step = {
            "id": "build",
            "command": "npm run build",
        }

        result = importer.convert_step(lobster_step)

        assert result.id == "build"
        assert result.exec == "npm run build"

    def test_stdin_step_stdout_maps_to_input_step_output(self) -> None:
        """Verify Lobster 'stdin: $step.stdout' maps to Gobby 'input: $step.output'."""
        from gobby.workflows.lobster_compat import LobsterImporter

        importer = LobsterImporter()

        lobster_step = {
            "id": "process",
            "command": "process-data",
            "stdin": "$build.stdout",
        }

        result = importer.convert_step(lobster_step)

        assert result.id == "process"
        assert result.exec == "process-data"
        assert result.input == "$build.output"

    def test_approval_true_maps_to_approval_required_true(self) -> None:
        """Verify Lobster 'approval: true' maps to Gobby 'approval: {required: true}'."""
        from gobby.workflows.lobster_compat import LobsterImporter

        importer = LobsterImporter()

        lobster_step = {
            "id": "deploy",
            "command": "deploy-app",
            "approval": True,
        }

        result = importer.convert_step(lobster_step)

        assert result.id == "deploy"
        assert result.exec == "deploy-app"
        assert result.approval is not None
        assert result.approval.required is True

    def test_condition_step_approved_preserved(self) -> None:
        """Verify Lobster 'condition: $step.approved' is preserved as condition string."""
        from gobby.workflows.lobster_compat import LobsterImporter

        importer = LobsterImporter()

        lobster_step = {
            "id": "post-deploy",
            "command": "notify-team",
            "condition": "$deploy.approved",
        }

        result = importer.convert_step(lobster_step)

        assert result.id == "post-deploy"
        assert result.exec == "notify-team"
        assert result.condition == "$deploy.approved"

    def test_approval_with_message(self) -> None:
        """Verify Lobster approval with message is preserved."""
        from gobby.workflows.lobster_compat import LobsterImporter

        importer = LobsterImporter()

        lobster_step = {
            "id": "deploy",
            "command": "deploy-app",
            "approval": {
                "required": True,
                "message": "Approve production deployment?",
            },
        }

        result = importer.convert_step(lobster_step)

        assert result.approval is not None
        assert result.approval.required is True
        assert result.approval.message == "Approve production deployment?"

    def test_stdin_with_complex_reference(self) -> None:
        """Verify complex stdin references are properly converted."""
        from gobby.workflows.lobster_compat import LobsterImporter

        importer = LobsterImporter()

        lobster_step = {
            "id": "analyze",
            "command": "analyze-output",
            "stdin": "$test_run.stdout",
        }

        result = importer.convert_step(lobster_step)

        assert result.input == "$test_run.output"

    def test_step_without_optional_fields(self) -> None:
        """Verify minimal step conversion works."""
        from gobby.workflows.lobster_compat import LobsterImporter

        importer = LobsterImporter()

        lobster_step = {
            "id": "simple",
            "command": "echo hello",
        }

        result = importer.convert_step(lobster_step)

        assert result.id == "simple"
        assert result.exec == "echo hello"
        assert result.input is None
        assert result.approval is None
        assert result.condition is None

    def test_multiple_stdin_stdout_conversions(self) -> None:
        """Verify all .stdout references are converted to .output."""
        from gobby.workflows.lobster_compat import LobsterImporter

        importer = LobsterImporter()

        # Test that pattern replacement handles underscores and various step names
        lobster_step = {
            "id": "merge",
            "command": "merge-outputs",
            "stdin": "$step_one.stdout",
        }

        result = importer.convert_step(lobster_step)

        assert result.input == "$step_one.output"


class TestLobsterImporterConvertPipeline:
    """Tests for LobsterImporter.convert_pipeline() method."""

    def test_convert_full_pipeline(self) -> None:
        """Verify full Lobster pipeline conversion."""
        from gobby.workflows.lobster_compat import LobsterImporter

        importer = LobsterImporter()

        lobster_pipeline = {
            "name": "ci-pipeline",
            "description": "CI/CD pipeline",
            "steps": [
                {"id": "build", "command": "npm run build"},
                {"id": "test", "command": "npm test", "stdin": "$build.stdout"},
                {"id": "deploy", "command": "deploy", "approval": True},
            ],
        }

        result = importer.convert_pipeline(lobster_pipeline)

        assert result.name == "ci-pipeline"
        assert result.description == "CI/CD pipeline"
        assert len(result.steps) == 3
        assert result.steps[0].exec == "npm run build"
        assert result.steps[1].input == "$build.output"
        assert result.steps[2].approval is not None
        assert result.steps[2].approval.required is True

    def test_convert_pipeline_preserves_metadata(self) -> None:
        """Verify pipeline metadata is preserved."""
        from gobby.workflows.lobster_compat import LobsterImporter

        importer = LobsterImporter()

        lobster_pipeline = {
            "name": "my-pipeline",
            "description": "Test description",
            "steps": [{"id": "step1", "command": "echo test"}],
        }

        result = importer.convert_pipeline(lobster_pipeline)

        assert result.name == "my-pipeline"
        assert result.description == "Test description"

    def test_convert_pipeline_passes_resume_on_restart(self) -> None:
        """Verify resume_on_restart is passed through from Lobster format."""
        from gobby.workflows.lobster_compat import LobsterImporter

        importer = LobsterImporter()

        lobster_pipeline = {
            "name": "resumable-pipeline",
            "steps": [{"id": "step1", "command": "echo test"}],
            "resume_on_restart": True,
        }

        result = importer.convert_pipeline(lobster_pipeline)
        assert result.resume_on_restart is True

    def test_convert_pipeline_resume_on_restart_defaults_false(self) -> None:
        """Verify resume_on_restart defaults to False when not specified."""
        from gobby.workflows.lobster_compat import LobsterImporter

        importer = LobsterImporter()

        lobster_pipeline = {
            "name": "basic-pipeline",
            "steps": [{"id": "step1", "command": "echo test"}],
        }

        result = importer.convert_pipeline(lobster_pipeline)
        assert result.resume_on_restart is False


class TestLobsterImporterImportFile:
    """Tests for LobsterImporter.import_file() method."""

    def test_import_file_reads_yaml(self, tmp_path: Path) -> None:
        """Verify import_file reads YAML from .lobster file."""
        from gobby.workflows.lobster_compat import LobsterImporter

        # Create a test .lobster file
        lobster_file = tmp_path / "test.lobster"
        lobster_file.write_text("""
name: file-pipeline
description: Pipeline from file
steps:
  - id: build
    command: npm run build
  - id: test
    command: npm test
""")

        importer = LobsterImporter()
        result = importer.import_file(lobster_file)

        assert result.name == "file-pipeline"
        assert result.description == "Pipeline from file"
        assert len(result.steps) == 2
        assert result.steps[0].id == "build"
        assert result.steps[0].exec == "npm run build"

    def test_import_file_converts_all_steps(self, tmp_path: Path) -> None:
        """Verify import_file converts all steps with field mappings."""
        from gobby.workflows.lobster_compat import LobsterImporter

        lobster_file = tmp_path / "pipeline.lobster"
        lobster_file.write_text("""
name: full-pipeline
description: Full conversion test
steps:
  - id: fetch
    command: fetch-data
  - id: process
    command: process-data
    stdin: $fetch.stdout
  - id: deploy
    command: deploy
    approval: true
    condition: $process.status == 'success'
""")

        importer = LobsterImporter()
        result = importer.import_file(lobster_file)

        assert len(result.steps) == 3
        # First step - basic
        assert result.steps[0].exec == "fetch-data"
        # Second step - stdin conversion
        assert result.steps[1].input == "$fetch.output"
        # Third step - approval and condition
        assert result.steps[2].approval is not None
        assert result.steps[2].approval.required is True
        assert result.steps[2].condition == "$process.status == 'success'"

    def test_import_file_maps_args_to_inputs(self, tmp_path: Path) -> None:
        """Verify import_file maps Lobster 'args' to Gobby 'inputs'."""
        from gobby.workflows.lobster_compat import LobsterImporter

        lobster_file = tmp_path / "with-args.lobster"
        lobster_file.write_text("""
name: args-pipeline
description: Pipeline with args
args:
  environment: staging
  version: "1.0.0"
steps:
  - id: deploy
    command: deploy --env $environment --version $version
""")

        importer = LobsterImporter()
        result = importer.import_file(lobster_file)

        assert result.name == "args-pipeline"
        assert result.inputs is not None
        assert result.inputs.get("environment") == "staging"
        assert result.inputs.get("version") == "1.0.0"

    def test_import_file_returns_valid_pipeline_definition(self, tmp_path: Path) -> None:
        """Verify import_file returns a valid PipelineDefinition."""
        from gobby.workflows.definitions import PipelineDefinition
        from gobby.workflows.lobster_compat import LobsterImporter

        lobster_file = tmp_path / "valid.lobster"
        lobster_file.write_text("""
name: valid-pipeline
description: Validation test
steps:
  - id: step1
    command: echo hello
""")

        importer = LobsterImporter()
        result = importer.import_file(lobster_file)

        assert isinstance(result, PipelineDefinition)
        assert result.name == "valid-pipeline"

    def test_import_file_handles_yaml_extension(self, tmp_path: Path) -> None:
        """Verify import_file also works with .yaml extension."""
        from gobby.workflows.lobster_compat import LobsterImporter

        yaml_file = tmp_path / "pipeline.yaml"
        yaml_file.write_text("""
name: yaml-pipeline
description: YAML file test
steps:
  - id: build
    command: make build
""")

        importer = LobsterImporter()
        result = importer.import_file(yaml_file)

        assert result.name == "yaml-pipeline"
        assert result.steps[0].exec == "make build"

    def test_import_file_path_as_string(self, tmp_path: Path) -> None:
        """Verify import_file accepts path as string."""
        from gobby.workflows.lobster_compat import LobsterImporter

        lobster_file = tmp_path / "string-path.lobster"
        lobster_file.write_text("""
name: string-path-pipeline
description: String path test
steps:
  - id: step1
    command: echo test
""")

        importer = LobsterImporter()
        # Pass as string instead of Path
        result = importer.import_file(str(lobster_file))

        assert result.name == "string-path-pipeline"
