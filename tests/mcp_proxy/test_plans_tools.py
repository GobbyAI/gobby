"""Tests for gobby-plans MCP registry."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from gobby.mcp_proxy.tools.plans import create_plan_registry
from gobby.storage.projects import LocalProjectManager

pytestmark = pytest.mark.unit


def _write_plan(root: Path) -> Path:
    plan_dir = root / ".gobby" / "plans"
    plan_dir.mkdir(parents=True)
    path = plan_dir / "task-100-demo.md"
    path.write_text(
        textwrap.dedent(
            """
            > **Plan ID:** task-100-demo

            ## P1 Phase
            `kind: framing`

            ### 1.1 Work [category: docs]
            `kind: deliverable`

            Body.

            **Acceptance:**
            - 1.1.1 — Docs exist. file: `docs/demo.md`
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_plan_tool_schemas_and_happy_path(temp_db, tmp_path: Path) -> None:
    project_id = LocalProjectManager(temp_db).create(name="plans", repo_path=str(tmp_path)).id
    plan_path = _write_plan(tmp_path)
    registry = create_plan_registry(temp_db, default_project_id=project_id)

    names = {tool["name"] for tool in registry.list_tools()}
    assert {
        "create_plan",
        "get_plan",
        "list_plans",
        "archive_plan",
        "update_plan_hash",
        "regenerate_coverage_manifest",
        "delete_plan",
    } <= names
    assert registry.get_schema("create_plan") is not None

    created = await registry.call(
        "create_plan",
        {
            "plan_id": "task-100-demo",
            "plan_path": str(plan_path),
            "root_task_ref": "#100",
        },
    )
    assert created["ok"] is True

    listed = await registry.call("list_plans", {"state": "active"})
    assert listed["count"] == 1
    assert listed["plans"][0]["plan_id"] == "task-100-demo"

    archived = await registry.call("archive_plan", {"plan_id": "task-100-demo"})
    assert archived["ok"] is True
    assert archived["plan"]["state"] == "archived"


@pytest.mark.asyncio
async def test_plan_tools_return_invalid_ref_for_blank_plan_ref(temp_db) -> None:
    registry = create_plan_registry(temp_db, default_project_id="project-1")

    result = await registry.call("get_plan", {"plan_id_or_ref": "   "})

    assert result["ok"] is False
    assert result["error"] == "invalid_ref"
