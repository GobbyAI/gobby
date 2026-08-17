"""Pipeline definition models."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class WebhookEndpoint(BaseModel):
    """Configuration for a webhook endpoint."""

    url: str
    method: str = "POST"
    headers: dict[str, str] = Field(default_factory=dict)


class WebhookConfig(BaseModel):
    """Webhook configuration for pipeline events."""

    on_approval_pending: WebhookEndpoint | None = None
    on_complete: WebhookEndpoint | None = None
    on_failure: WebhookEndpoint | None = None


class PipelineApproval(BaseModel):
    """Approval gate configuration for a pipeline step."""

    required: bool = False
    message: str | None = None
    timeout_seconds: int | None = None


class MCPStepConfig(BaseModel):
    """Configuration for an MCP tool call step in a pipeline."""

    server: str
    tool: str
    arguments: dict[str, Any] | None = None


class PipelineStep(BaseModel):
    """A single step in a pipeline workflow.

    Steps must have exactly one execution type: exec, prompt, invoke_pipeline, mcp, or wait.
    """

    id: str

    # Execution types (mutually exclusive - exactly one required)
    exec: str | None = None  # Shell command to run
    prompt: str | None = None  # LLM prompt template
    invoke_pipeline: str | dict[str, Any] | None = None  # Name of pipeline to invoke
    mcp: MCPStepConfig | None = None  # Call MCP tool directly
    wait: dict[str, Any] | None = None  # Block until completion event fires

    # Optional fields
    condition: str | None = None  # Condition for step execution
    approval: PipelineApproval | None = None  # Approval gate
    tools: list[str] = Field(default_factory=list)  # Tool restrictions for prompt steps
    input: str | None = None  # Explicit input reference (e.g., $prev_step.output)
    timeout_seconds: float | str | None = None  # Positive exec timeout or template

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def validate_timeout_seconds(cls, value: Any) -> float | str | None:
        """Validate a positive timeout or a full template expression."""
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("timeout_seconds must be a positive number")
        if isinstance(value, (int, float)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("timeout_seconds must be greater than 0")
            return float(value)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("${{") and stripped.endswith("}}") and stripped[3:-2].strip():
                return value
        raise ValueError("timeout_seconds must be a positive number or template expression")

    @model_validator(mode="before")
    @classmethod
    def reject_activate_workflow(cls, data: Any) -> Any:
        """Reject the removed pipeline-only workflow activation step."""
        if isinstance(data, dict) and "activate_workflow" in data:
            raise ValueError("activate_workflow is not a supported pipeline step type")
        return data

    @model_validator(mode="after")
    def validate_exactly_one_execution_type(self) -> PipelineStep:
        """Validate that exactly one execution type is specified."""
        exec_types = [
            self.exec,
            self.prompt,
            self.invoke_pipeline,
            self.mcp,
            self.wait,
        ]
        specified = [t for t in exec_types if t is not None]

        if len(specified) == 0:
            raise ValueError(
                "PipelineStep requires at least one execution type: "
                "exec, prompt, invoke_pipeline, mcp, or wait"
            )
        if len(specified) > 1:
            raise ValueError(
                "PipelineStep exec, prompt, invoke_pipeline, mcp, and wait are mutually "
                "exclusive - only one allowed"
            )
        return self


class PipelineDefinition(BaseModel):
    """Definition for a pipeline workflow with typed data flow between steps.

    Pipelines execute steps sequentially with explicit data flow via $step.output references.
    """

    name: str
    description: str | None = None
    version: str = "1.0"
    type: Literal["pipeline"] = "pipeline"
    enabled: bool = True
    priority: int = 100
    deprecated: bool = False
    deprecated_reason: str | None = None

    @field_validator("version", mode="before")
    @classmethod
    def coerce_version_to_string(cls, v: Any) -> str:
        """Accept numeric versions (1.0, 2) and coerce to string."""
        return str(v) if v is not None else "1.0"

    # Input/output schema
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)

    # Pipeline steps
    steps: list[PipelineStep] = Field(default_factory=list)

    # Webhook notifications
    webhooks: WebhookConfig | None = None

    # Expose as MCP tool
    expose_as_tool: bool = False

    # Resume execution after daemon restart (opt-in, steps must be idempotent)
    resume_on_restart: bool = False

    @field_validator("steps", mode="after")
    @classmethod
    def validate_steps(cls, v: list[PipelineStep]) -> list[PipelineStep]:
        """Validate pipeline steps."""
        ids = [step.id for step in v]
        if len(ids) != len(set(ids)):
            duplicates = [id for id in ids if ids.count(id) > 1]
            raise ValueError(f"Pipeline step IDs must be unique. Duplicates: {set(duplicates)}")

        return v

    @model_validator(mode="after")
    def validate_active_pipeline_has_steps(self) -> PipelineDefinition:
        """Require every pipeline definition to include executable steps."""
        if not self.steps and not self.deprecated:
            raise ValueError("Pipeline requires at least one step")
        return self

    def get_step(self, step_id: str) -> PipelineStep | None:
        """Get a step by its ID."""
        for step in self.steps:
            if step.id == step_id:
                return step
        return None
