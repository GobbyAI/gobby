"""Computed Gobby-experience survey flag for rule evaluation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

GOBBY_PROJECT_NAME = "gobby"
SURVEY_ACTIVE_VARIABLE = "_gobby_feedback_survey_active"
SURVEY_CONFIG_KEY = "session_feedback.survey"


def survey_is_active(scope: str, project_name: str) -> bool:
    """Return whether survey gates should fire for this project and config scope."""
    normalized = (scope or "gobby").strip().lower() or "gobby"
    if normalized == "all":
        return True
    if normalized == "gobby":
        return project_name == GOBBY_PROJECT_NAME
    return False


def inject_survey_active(
    variables: dict[str, Any],
    config_values: Mapping[str, object],
) -> None:
    """Set the per-event survey flag from daemon config and project name."""
    project_name = ""
    project_info = variables.get("project")
    if isinstance(project_info, dict):
        project_name = str(project_info.get("name") or "")
    variables[SURVEY_ACTIVE_VARIABLE] = survey_is_active(
        str(config_values.get(SURVEY_CONFIG_KEY, "gobby")),
        project_name,
    )
