"""Lobster compatibility utilities for pipeline migration.

This module provides utilities to convert Lobster-format pipelines
to Gobby's native PipelineDefinition format.

Lobster field mappings:
- command → exec
- stdin: $step.stdout → input: $step.output
- approval: true → approval: {required: true}
- condition: $step.approved → condition (preserved as string)
"""

import re
from pathlib import Path
from typing import Any

import yaml

from gobby.workflows.definitions import PipelineApproval, PipelineDefinition, PipelineStep


class LobsterImporter:
    """Converts Lobster-format pipelines to Gobby PipelineDefinition.

    Example:
        importer = LobsterImporter()
        lobster_step = {"id": "build", "command": "npm run build"}
        gobby_step = importer.convert_step(lobster_step)
    """

    def convert_step(self, lobster_step: dict[str, Any]) -> PipelineStep:
        """Convert a Lobster step to a Gobby PipelineStep.

        Args:
            lobster_step: Dictionary with Lobster step format

        Returns:
            PipelineStep with converted fields

        Field mappings:
            - id: preserved as-is
            - command → exec
            - stdin: $step.stdout → input: $step.output
            - approval: true → approval: {required: true}
            - approval: {required: true, message: "..."} → approval object
            - condition: preserved as-is
        """
        step_id = lobster_step.get("id", "")

        # Map command → exec
        exec_cmd = lobster_step.get("command")

        # Map stdin: $step.stdout → input: $step.output
        input_ref = None
        stdin_value = lobster_step.get("stdin")
        if stdin_value:
            # Replace .stdout with .output in the reference
            input_ref = re.sub(r"\.stdout\b", ".output", stdin_value)

        # Map approval
        approval = None
        approval_value = lobster_step.get("approval")
        if approval_value is True:
            # Simple boolean approval
            approval = PipelineApproval(required=True)
        elif isinstance(approval_value, dict):
            # Detailed approval object
            approval = PipelineApproval(
                required=approval_value.get("required", True),
                message=approval_value.get("message"),
                timeout_seconds=approval_value.get("timeout_seconds"),
            )

        # Preserve condition as-is
        condition = lobster_step.get("condition")

        return PipelineStep(
            id=step_id,
            exec=exec_cmd,
            input=input_ref,
            approval=approval,
            condition=condition,
        )

    def convert_pipeline(self, lobster_pipeline: dict[str, Any]) -> PipelineDefinition:
        """Convert a Lobster pipeline to a Gobby PipelineDefinition.

        Args:
            lobster_pipeline: Dictionary with Lobster pipeline format

        Returns:
            PipelineDefinition with converted steps

        Field mappings:
            - name: preserved as-is
            - description: preserved as-is
            - args → inputs
            - steps: converted via convert_step()
        """
        name = lobster_pipeline.get("name", "")
        description = lobster_pipeline.get("description", "")

        # Map args → inputs
        inputs = lobster_pipeline.get("args", {})

        steps = []
        for lobster_step in lobster_pipeline.get("steps", []):
            steps.append(self.convert_step(lobster_step))

        resume_on_restart = lobster_pipeline.get("resume_on_restart", False)

        return PipelineDefinition(
            name=name,
            description=description,
            inputs=inputs,
            steps=steps,
            resume_on_restart=resume_on_restart,
        )

    def import_file(self, path: str | Path) -> PipelineDefinition:
        """Import a Lobster pipeline from a YAML file.

        Args:
            path: Path to the .lobster or .yaml file

        Returns:
            PipelineDefinition with converted pipeline

        Raises:
            FileNotFoundError: If the pipeline file doesn't exist
            ValueError: If the file cannot be read or parsed

        Example:
            importer = LobsterImporter()
            pipeline = importer.import_file("ci.lobster")
        """
        file_path = Path(path)
        try:
            content = file_path.read_text()
        except FileNotFoundError:
            raise FileNotFoundError(f"Pipeline file not found: {path}") from None
        except UnicodeDecodeError as e:
            raise ValueError(f"Cannot read pipeline file {path}: {e}") from e

        try:
            lobster_pipeline = yaml.safe_load(content)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in pipeline file {path}: {e}") from e

        return self.convert_pipeline(lobster_pipeline)
