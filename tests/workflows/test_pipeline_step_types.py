"""Tests for removed pipeline step types."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


class TestPipelineStepValidation:
    """Tests for PipelineStep rejecting removed execution types."""

    @pytest.mark.parametrize(
        "step",
        [
            {"id": "activate", "activate_workflow": {"name": "auto-task"}},
            {
                "id": "activate",
                "prompt": "Do something",
                "activate_workflow": {"name": "auto-task"},
            },
        ],
    )
    def test_activate_workflow_rejected_at_definition_load(self, step: dict[str, object]) -> None:
        """Pipeline definitions reject activate_workflow before execution."""
        from gobby.workflows.definitions import PipelineDefinition

        with pytest.raises(
            ValueError, match="activate_workflow is not a supported pipeline step type"
        ):
            PipelineDefinition.model_validate(
                {
                    "name": "invalid-pipeline",
                    "type": "pipeline",
                    "steps": [step],
                }
            )

    def test_spawn_session_rejected(self) -> None:
        """PipelineStep no longer accepts spawn_session as an execution type."""
        from gobby.workflows.definitions import PipelineStep

        with pytest.raises(ValueError, match="requires at least one execution type"):
            PipelineStep(
                id="spawn",
                spawn_session={"cli": "claude", "prompt": "Do work"},
            )
