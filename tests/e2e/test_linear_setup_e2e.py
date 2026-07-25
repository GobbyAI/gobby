"""Stubbed Linear MCP E2E for guided setup and project-scoped sync."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.cli.linear import _run_linear_setup
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.e2e


class StubLinearMCP:
    def __init__(self) -> None:
        self.health = {"linear": MagicMock(state="connected")}
        self.teams = [{"id": "team-1", "name": "Engineering", "key": "ENG"}]
        self.projects: dict[str, dict[str, Any]] = {}
        self.issues: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def has_server(self, server_name: str) -> bool:
        return server_name == "linear"

    async def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        assert server_name == "linear"
        self.calls.append((tool_name, dict(arguments)))

        if tool_name == "list_teams":
            return {"teams": self.teams}

        if tool_name == "list_projects":
            team_id = arguments["teamId"]
            return {
                "projects": [
                    project for project in self.projects.values() if project["teamId"] == team_id
                ]
            }

        if tool_name == "create_project":
            project_id = f"proj-{len(self.projects) + 1}"
            project = {
                "id": project_id,
                "name": arguments["name"],
                "teamId": arguments["teamId"],
            }
            self.projects[project_id] = project
            return {"project": project}

        if tool_name == "create_issue":
            issue_id = f"issue-{len(self.issues) + 1}"
            issue = {
                "id": issue_id,
                "title": arguments["title"],
                "description": arguments.get("description", ""),
                "priority": arguments.get("priority", 2),
                "teamId": arguments["teamId"],
                "projectId": arguments.get("projectId"),
                "updatedAt": "2026-05-05T00:00:00+00:00",
                "state": {"name": "Todo"},
            }
            self.issues[issue_id] = issue
            return {"id": issue_id, "title": issue["title"]}

        if tool_name == "list_issues":
            team_id = arguments["teamId"]
            project_id = arguments.get("projectId")
            issues = [
                issue
                for issue in self.issues.values()
                if issue["teamId"] == team_id
                and (project_id is None or issue["projectId"] == project_id)
            ]
            return {"issues": issues}

        if tool_name == "update_issue":
            issue_id = arguments.get("id") or arguments.get("issueId")
            issue = self.issues[issue_id]
            issue.update(
                {
                    key: arguments[key]
                    for key in ("title", "description", "priority")
                    if key in arguments
                }
            )
            issue["updatedAt"] = "2026-05-05T00:00:02+00:00"
            return {"id": issue_id, "title": issue["title"]}

        raise AssertionError(f"Unexpected Linear tool: {tool_name}")


@pytest.mark.asyncio
async def test_linear_setup_stubbed_mcp_e2e(temp_db, tmp_path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".gobby").mkdir()

    project_manager = LocalProjectManager(temp_db)
    task_manager = LocalTaskManager(temp_db)
    project = project_manager.create(name="gobby-e2e", repo_path=str(project_root))
    (project_root / ".gobby" / "project.json").write_text(
        json.dumps(
            {
                "id": project.id,
                "name": project.name,
                "created_at": project.created_at.isoformat(),
            }
        )
    )
    linear = StubLinearMCP()

    setup_result = await _run_linear_setup(
        task_manager=task_manager,
        mcp_manager=linear,
        project_manager=project_manager,
        project_id=project.id,
        bootstrap=True,
        team_id=None,
        linear_project_id=None,
        project_name="Gobby E2E",
    )

    assert setup_result["linear_team_id"] == "team-1"
    assert setup_result["linear_project_id"] == "proj-1"
    stored = project_manager.get(project.id)
    assert stored is not None
    assert stored.linear_team_id == "team-1"
    assert stored.linear_project_id == "proj-1"
    persisted = json.loads((project_root / ".gobby" / "project.json").read_text())
    assert persisted["linear_project_id"] == "proj-1"

    assert any(tool == "list_teams" for tool, _args in linear.calls)
    assert any(tool == "create_project" for tool, _args in linear.calls)
