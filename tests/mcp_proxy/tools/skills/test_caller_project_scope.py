"""Skill reads act on the caller's project, not the daemon's registry project (#25775)."""

import uuid
from pathlib import Path

import pytest

from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration


def _insert_project(db: HubDatabase, project_id: str, name: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) "
            "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (project_id, name),
        )


@pytest.mark.asyncio
async def test_get_skill_from_project_session_serves_that_projects_row(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """A project-scoped install from project B is readable from B's session, not the daemon's."""
    from gobby.mcp_proxy.tools.skills import create_skills_registry
    from gobby.utils.project_context import reset_project_context, set_project_context

    daemon_project_id = str(uuid.uuid4())
    caller_project_id = str(uuid.uuid4())
    _insert_project(temp_db, daemon_project_id, "daemon-project")
    _insert_project(temp_db, caller_project_id, "caller-project")

    skill_dir = tmp_path / "caller-only"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        '---\nname: caller-only\ndescription: Only the caller project has this\nversion: "1.0.0"\n'
        "---\n\n# Caller Only\n\nCaller project body.\n",
        encoding="utf-8",
    )

    registry = create_skills_registry(temp_db, project_id=daemon_project_id)
    install = registry.get_tool("install_skill")
    get_skill = registry.get_tool("get_skill")
    assert install is not None
    assert get_skill is not None

    token = set_project_context({"id": caller_project_id})
    try:
        installed = await install(source=str(skill_dir), project_scoped=True)
        assert installed["success"] is True
        from_caller = await get_skill(name="caller-only", brief=False)
    finally:
        reset_project_context(token)

    assert from_caller["success"] is True
    assert "Caller project body." in from_caller["skill"]["content"]

    # Without a caller project the call resolves to the daemon's project, which has no such row.
    from_daemon = await get_skill(name="caller-only", brief=False)
    assert from_daemon["success"] is False
