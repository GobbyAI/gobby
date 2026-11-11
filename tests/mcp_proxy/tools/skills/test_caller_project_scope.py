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


def _write_override_skill(folder: Path, name: str) -> Path:
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: Explicit override scope check.\nversion: "1.0.0"\n'
        "---\n\n# Override Scope\n\nOverride body.\n",
        encoding="utf-8",
    )
    return folder


@pytest.mark.asyncio
async def test_explicit_project_override_that_differs_from_the_session_is_refused(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """An explicit project_id naming another project than the session's is refused, never honored."""
    from gobby.mcp_proxy.tools.skills import create_skills_registry
    from gobby.storage.skills import LocalSkillManager
    from gobby.utils.project_context import reset_project_context, set_project_context
    from gobby.utils.session_context import (
        SessionContext,
        reset_session_context,
        set_session_context,
    )

    daemon_project_id = str(uuid.uuid4())
    session_project_id = str(uuid.uuid4())
    override_project_id = str(uuid.uuid4())
    _insert_project(temp_db, daemon_project_id, "daemon-project")
    _insert_project(temp_db, session_project_id, "session-project")
    _insert_project(temp_db, override_project_id, "override-project")
    skill_dir = _write_override_skill(tmp_path / "override-only", "override-only")

    registry = create_skills_registry(temp_db, project_id=daemon_project_id)
    session_token = set_session_context(
        SessionContext(session_id=str(uuid.uuid4()), project_id=session_project_id)
    )
    project_token = set_project_context({"id": override_project_id})
    try:
        result = await registry.call(
            "install_skill", {"source": str(skill_dir), "project_scoped": True}
        )
    finally:
        reset_project_context(project_token)
        reset_session_context(session_token)

    assert result["success"] is False
    assert override_project_id in result["error"]
    assert session_project_id in result["error"]
    storage = LocalSkillManager(temp_db)
    for project_id in (override_project_id, session_project_id):
        assert (
            storage.get_by_name("override-only", project_id=project_id, include_global=False)
            is None
        )


@pytest.mark.asyncio
async def test_explicit_project_override_matching_the_session_passes(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """An explicit project_id that names the session's own project is honored."""
    from gobby.mcp_proxy.tools.skills import create_skills_registry
    from gobby.storage.skills import LocalSkillManager
    from gobby.utils.project_context import reset_project_context, set_project_context
    from gobby.utils.session_context import (
        SessionContext,
        reset_session_context,
        set_session_context,
    )

    daemon_project_id = str(uuid.uuid4())
    session_project_id = str(uuid.uuid4())
    _insert_project(temp_db, daemon_project_id, "daemon-project")
    _insert_project(temp_db, session_project_id, "session-project")
    skill_dir = _write_override_skill(tmp_path / "matching-only", "matching-only")

    registry = create_skills_registry(temp_db, project_id=daemon_project_id)
    session_token = set_session_context(
        SessionContext(session_id=str(uuid.uuid4()), project_id=session_project_id)
    )
    project_token = set_project_context({"id": session_project_id})
    try:
        result = await registry.call(
            "install_skill", {"source": str(skill_dir), "project_scoped": True}
        )
    finally:
        reset_project_context(project_token)
        reset_session_context(session_token)

    assert result["success"] is True
    installed = LocalSkillManager(temp_db).get_by_name(
        "matching-only", project_id=session_project_id, include_global=False
    )
    assert installed is not None
