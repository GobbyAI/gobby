"""Tests for Pipeline workflow definition models.

TDD tests for PipelineDefinition, PipelineStep, PipelineApproval, and WebhookConfig.
"""

import pytest
from pydantic import ValidationError

from gobby.workflows.definitions import (
    PipelineApproval,
    PipelineDefinition,
    PipelineStep,
    WebhookConfig,
    WebhookEndpoint,
)

pytestmark = pytest.mark.unit


class TestWebhookEndpoint:
    """Tests for WebhookEndpoint model."""

    def test_minimal_endpoint(self) -> None:
        """Test creating endpoint with just URL."""
        endpoint = WebhookEndpoint(url="https://example.com/webhook")
        assert endpoint.url == "https://example.com/webhook"
        assert endpoint.method == "POST"  # default
        assert endpoint.headers == {}

    def test_full_endpoint(self) -> None:
        """Test creating endpoint with all fields."""
        endpoint = WebhookEndpoint(
            url="https://example.com/webhook",
            method="PUT",
            headers={"Authorization": "Bearer token123"},
        )
        assert endpoint.url == "https://example.com/webhook"
        assert endpoint.method == "PUT"
        assert endpoint.headers == {"Authorization": "Bearer token123"}


class TestWebhookConfig:
    """Tests for WebhookConfig model."""

    def test_empty_config(self) -> None:
        """Test creating empty webhook config."""
        config = WebhookConfig()
        assert config.on_approval_pending is None
        assert config.on_complete is None
        assert config.on_failure is None

    def test_full_config(self) -> None:
        """Test creating webhook config with all hooks."""
        config = WebhookConfig(
            on_approval_pending=WebhookEndpoint(url="https://example.com/approval"),
            on_complete=WebhookEndpoint(url="https://example.com/complete"),
            on_failure=WebhookEndpoint(url="https://example.com/failure"),
        )
        assert config.on_approval_pending is not None
        assert config.on_approval_pending.url == "https://example.com/approval"
        assert config.on_complete is not None
        assert config.on_failure is not None


class TestPipelineApproval:
    """Tests for PipelineApproval model."""

    def test_minimal_approval(self) -> None:
        """Test creating approval with required=True."""
        approval = PipelineApproval(required=True)
        assert approval.required is True
        assert approval.message is None
        assert approval.timeout_seconds is None

    def test_full_approval(self) -> None:
        """Test creating approval with all fields."""
        approval = PipelineApproval(
            required=True,
            message="Please review the changes before proceeding.",
            timeout_seconds=3600,
        )
        assert approval.required is True
        assert approval.message == "Please review the changes before proceeding."
        assert approval.timeout_seconds == 3600

    def test_approval_not_required(self) -> None:
        """Test creating approval with required=False."""
        approval = PipelineApproval(required=False)
        assert approval.required is False


class TestPipelineStep:
    """Tests for PipelineStep model."""

    def test_exec_step(self) -> None:
        """Test creating a step with exec field."""
        step = PipelineStep(id="run_tests", exec="pytest tests/ -v")
        assert step.id == "run_tests"
        assert step.exec == "pytest tests/ -v"
        assert step.prompt is None
        assert step.invoke_pipeline is None
        assert step.condition is None
        assert step.approval is None
        assert step.tools == []

    def test_prompt_step(self) -> None:
        """Test creating a step with prompt field."""
        step = PipelineStep(
            id="analyze",
            prompt="Analyze the test results from $run_tests.output",
            tools=["read_file", "search_code"],
        )
        assert step.id == "analyze"
        assert step.prompt == "Analyze the test results from $run_tests.output"
        assert step.exec is None
        assert step.tools == ["read_file", "search_code"]

    def test_invoke_pipeline_step(self) -> None:
        """Test creating a step that invokes another pipeline."""
        step = PipelineStep(id="run_review", invoke_pipeline="code-review")
        assert step.id == "run_review"
        assert step.invoke_pipeline == "code-review"
        assert step.exec is None
        assert step.prompt is None

    def test_step_with_condition(self) -> None:
        """Test creating a step with a condition."""
        step = PipelineStep(
            id="deploy",
            exec="./deploy.sh",
            condition="$run_tests.output.exit_code == 0",
        )
        assert step.id == "deploy"
        assert step.condition == "$run_tests.output.exit_code == 0"

    def test_step_with_approval(self) -> None:
        """Test creating a step with approval gate."""
        step = PipelineStep(
            id="production_deploy",
            exec="./deploy.sh --env=production",
            approval=PipelineApproval(required=True, message="Approve production deployment?"),
        )
        assert step.id == "production_deploy"
        assert step.approval is not None
        assert step.approval.required is True
        assert step.approval.message == "Approve production deployment?"

    def test_step_with_input(self) -> None:
        """Test creating a step with explicit input reference."""
        step = PipelineStep(
            id="process",
            exec="python process.py",
            input="$previous_step.output",
        )
        assert step.id == "process"
        assert step.input == "$previous_step.output"

    def test_step_mutually_exclusive_validation(self) -> None:
        """Test that exec, prompt, and invoke_pipeline are mutually exclusive."""
        # This should raise ValidationError - only one execution type allowed
        with pytest.raises(ValidationError) as exc_info:
            PipelineStep(id="invalid", exec="echo hello", prompt="Say hello")
        assert (
            "mutually exclusive" in str(exc_info.value).lower()
            or "only one" in str(exc_info.value).lower()
        )

    def test_step_requires_one_execution_type(self) -> None:
        """Test that at least one execution type is required."""
        with pytest.raises(ValidationError) as exc_info:
            PipelineStep(id="empty")
        assert (
            "at least one" in str(exc_info.value).lower()
            or "required" in str(exc_info.value).lower()
        )


class TestPipelineDefinition:
    """Tests for PipelineDefinition model."""

    def test_minimal_pipeline(self) -> None:
        """Test creating minimal valid pipeline."""
        pipeline = PipelineDefinition(
            name="simple-pipeline",
            steps=[PipelineStep(id="step1", exec="echo hello")],
        )
        assert pipeline.name == "simple-pipeline"
        assert pipeline.type == "pipeline"
        assert pipeline.version == "1.0"
        assert len(pipeline.steps) == 1
        assert pipeline.inputs == {}
        assert pipeline.outputs == {}
        assert pipeline.resume_on_restart is False

    def test_resume_on_restart_defaults_false(self) -> None:
        """resume_on_restart defaults to False."""
        pipeline = PipelineDefinition(
            name="test",
            steps=[PipelineStep(id="s1", exec="true")],
        )
        assert pipeline.resume_on_restart is False

    def test_resume_on_restart_opt_in(self) -> None:
        """resume_on_restart can be set to True."""
        pipeline = PipelineDefinition(
            name="resumable",
            steps=[PipelineStep(id="s1", exec="true")],
            resume_on_restart=True,
        )
        assert pipeline.resume_on_restart is True

    def test_full_pipeline(self) -> None:
        """Test creating pipeline with all fields."""
        pipeline = PipelineDefinition(
            name="full-pipeline",
            description="A fully specified pipeline",
            version="2.0",
            inputs={
                "files": {"type": "array", "description": "Files to process"},
                "mode": {"type": "string", "default": "fast"},
            },
            outputs={
                "result": "$final_step.output",
                "summary": "$analyze.output.summary",
            },
            steps=[
                PipelineStep(id="step1", exec="echo $inputs.mode"),
                PipelineStep(id="step2", prompt="Process files: $inputs.files"),
            ],
            webhooks=WebhookConfig(on_complete=WebhookEndpoint(url="https://example.com/done")),
        )
        assert pipeline.name == "full-pipeline"
        assert pipeline.description == "A fully specified pipeline"
        assert pipeline.version == "2.0"
        assert pipeline.type == "pipeline"
        assert "files" in pipeline.inputs
        assert "result" in pipeline.outputs
        assert len(pipeline.steps) == 2
        assert pipeline.webhooks is not None
        assert pipeline.webhooks.on_complete is not None

    def test_pipeline_type_is_fixed(self) -> None:
        """Test that pipeline type is always 'pipeline'."""
        pipeline = PipelineDefinition(
            name="test",
            steps=[PipelineStep(id="s1", exec="true")],
        )
        assert pipeline.type == "pipeline"

    def test_version_coercion(self) -> None:
        """Test that numeric versions are coerced to strings."""
        pipeline = PipelineDefinition(
            name="test",
            version=1.0,  # type: ignore - testing coercion
            steps=[PipelineStep(id="s1", exec="true")],
        )
        assert pipeline.version == "1.0"
        assert isinstance(pipeline.version, str)

    def test_pipeline_requires_steps(self) -> None:
        """Test that pipeline requires at least one step."""
        with pytest.raises(ValidationError):
            PipelineDefinition(name="empty", steps=[])

    def test_pipeline_step_ids_unique(self) -> None:
        """Test that step IDs must be unique within pipeline."""
        with pytest.raises(ValidationError) as exc_info:
            PipelineDefinition(
                name="duplicate-ids",
                steps=[
                    PipelineStep(id="step1", exec="echo 1"),
                    PipelineStep(id="step1", exec="echo 2"),  # duplicate
                ],
            )
        assert "unique" in str(exc_info.value).lower() or "duplicate" in str(exc_info.value).lower()

    def test_get_step_by_id(self) -> None:
        """Test getting a step by its ID."""
        pipeline = PipelineDefinition(
            name="test",
            steps=[
                PipelineStep(id="first", exec="echo 1"),
                PipelineStep(id="second", exec="echo 2"),
            ],
        )
        step = pipeline.get_step("second")
        assert step is not None
        assert step.id == "second"

    def test_get_step_not_found(self) -> None:
        """Test getting a step that doesn't exist."""
        pipeline = PipelineDefinition(
            name="test",
            steps=[PipelineStep(id="only", exec="echo 1")],
        )
        step = pipeline.get_step("nonexistent")
        assert step is None


class TestStepOutputReferences:
    """Tests for $step.output reference pattern validation."""

    def test_valid_output_reference_in_prompt(self) -> None:
        """Test that $step.output references are valid in prompts."""
        pipeline = PipelineDefinition(
            name="test",
            steps=[
                PipelineStep(id="step1", exec="echo hello"),
                PipelineStep(id="step2", prompt="Process: $step1.output"),
            ],
        )
        assert len(pipeline.steps) == 2
        assert "$step1.output" in pipeline.steps[1].prompt  # type: ignore

    def test_valid_nested_output_reference(self) -> None:
        """Test that nested $step.output.field references are valid."""
        pipeline = PipelineDefinition(
            name="test",
            steps=[
                PipelineStep(id="analyze", exec="./analyze.sh"),
                PipelineStep(
                    id="report",
                    prompt="Summary: $analyze.output.summary, Score: $analyze.output.score",
                ),
            ],
        )
        assert "$analyze.output.summary" in pipeline.steps[1].prompt  # type: ignore

    def test_output_reference_in_condition(self) -> None:
        """Test that $step.output references work in conditions."""
        pipeline = PipelineDefinition(
            name="test",
            steps=[
                PipelineStep(id="test", exec="pytest"),
                PipelineStep(
                    id="deploy",
                    exec="./deploy.sh",
                    condition="$test.output.exit_code == 0",
                ),
            ],
        )
        assert "$test.output.exit_code" in pipeline.steps[1].condition  # type: ignore

    def test_output_reference_in_pipeline_outputs(self) -> None:
        """Test that $step.output references work in pipeline outputs."""
        pipeline = PipelineDefinition(
            name="test",
            outputs={
                "final": "$last_step.output",
                "summary": "$analyze.output.summary",
            },
            steps=[
                PipelineStep(id="analyze", exec="./analyze.sh"),
                PipelineStep(id="last_step", exec="./finish.sh"),
            ],
        )
        assert pipeline.outputs["final"] == "$last_step.output"
        assert pipeline.outputs["summary"] == "$analyze.output.summary"

    def test_inputs_reference_in_step(self) -> None:
        """Test that $inputs references work in steps."""
        pipeline = PipelineDefinition(
            name="test",
            inputs={"target": {"type": "string"}},
            steps=[
                PipelineStep(id="process", exec="./process.sh $inputs.target"),
            ],
        )
        assert "$inputs.target" in pipeline.steps[0].exec  # type: ignore
