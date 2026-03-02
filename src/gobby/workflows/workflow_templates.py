"""
Workflow definition templates for the visual builder 'New' button.

Provides pre-built workflow and pipeline templates that users can
instantiate as starting points.
"""

import json
from typing import Any


def get_workflow_templates() -> list[dict[str, Any]]:
    """Return all available workflow templates.

    Each template includes:
    - id: unique identifier
    - name: display name
    - description: brief description
    - workflow_type: 'workflow' or 'pipeline'
    - definition_json: JSON string of the full definition
    """
    return [
        _blank_workflow(),
        _tdd_developer(),
        _blank_pipeline(),
        _ci_pipeline(),
    ]


def get_template_by_id(template_id: str) -> dict[str, Any] | None:
    """Get a specific template by ID."""
    for t in get_workflow_templates():
        if t["id"] == template_id:
            return t
    return None


def _blank_workflow() -> dict[str, Any]:
    definition = {
        "name": "",
        "description": "",
        "version": "1.0",
        "steps": [
            {
                "name": "work",
                "description": "Main work step",
                "allowed_tools": "all",
            }
        ],
    }
    return {
        "id": "blank-workflow",
        "name": "Blank Workflow",
        "description": "Empty workflow with a single work step",
        "workflow_type": "workflow",
        "definition_json": json.dumps(definition),
    }


def _tdd_developer() -> dict[str, Any]:
    definition = {
        "name": "",
        "description": "TDD red/green/blue cycle workflow",
        "version": "1.0",
        "steps": [
            {
                "name": "red",
                "description": "Write a failing test",
                "status_message": "Writing failing test...",
                "allowed_tools": ["Edit", "Write", "Read", "Glob", "Grep"],
                "transitions": [{"to": "green", "when": "test written"}],
            },
            {
                "name": "green",
                "description": "Write minimal code to pass the test",
                "status_message": "Making test pass...",
                "allowed_tools": ["Edit", "Write", "Read", "Glob", "Grep", "Bash"],
                "transitions": [{"to": "blue", "when": "tests pass"}],
            },
            {
                "name": "blue",
                "description": "Refactor while keeping tests green",
                "status_message": "Refactoring...",
                "allowed_tools": "all",
                "transitions": [{"to": "red", "when": "refactor complete"}],
            },
        ],
        "exit_condition": "steps.all_complete",
    }
    return {
        "id": "tdd-developer",
        "name": "TDD Developer",
        "description": "Red/green/blue cycle with tool restrictions per phase",
        "workflow_type": "workflow",
        "definition_json": json.dumps(definition),
    }


def _blank_pipeline() -> dict[str, Any]:
    definition = {
        "name": "",
        "type": "pipeline",
        "description": "",
        "version": "1.0",
        "steps": [
            {"id": "step-1", "exec": "echo 'hello world'"},
        ],
    }
    return {
        "id": "blank-pipeline",
        "name": "Blank Pipeline",
        "description": "Empty sequential pipeline with one step",
        "workflow_type": "pipeline",
        "definition_json": json.dumps(definition),
    }


def _ci_pipeline() -> dict[str, Any]:
    definition = {
        "name": "",
        "type": "pipeline",
        "description": "CI pipeline with build, test, and deploy stages",
        "version": "1.0",
        "steps": [
            {
                "id": "build",
                "exec": "echo 'Building...'",
            },
            {
                "id": "test",
                "exec": "echo 'Running tests...'",
                "condition": "steps.build.success",
            },
            {
                "id": "approval",
                "exec": "echo 'Approved - deploying'",
                "approval": {
                    "required": True,
                    "message": "Deploy to production?",
                },
            },
            {
                "id": "deploy",
                "exec": "echo 'Deploying...'",
            },
        ],
    }
    return {
        "id": "ci-pipeline",
        "name": "CI Pipeline Template",
        "description": "Build/test/deploy pipeline with approval gate",
        "workflow_type": "pipeline",
        "definition_json": json.dumps(definition),
    }
