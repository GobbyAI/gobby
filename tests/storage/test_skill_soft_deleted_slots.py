"""A soft-deleted skill row does not block a same-name install or move (#25775)."""

import uuid
from pathlib import Path

import pytest

from gobby.mcp_proxy.tools.skills import create_skills_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.skills import LocalSkillManager
from gobby.utils.project_context import reset_project_context, set_project_context

pytestmark = pytest.mark.integration


def _insert_project(db: HubDatabase, project_id: str, name: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) "
            "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (project_id, name),
        )


def _write_skill(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: Slot test {name}\nversion: "1.0.0"\n---\n\n'
        f"# {name}\n\nBody.\n",
        encoding="utf-8",
    )
    return folder


@pytest.mark.asyncio
async def test_soft_deleted_project_row_does_not_block_reinstall(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_id = str(uuid.uuid4())
    _insert_project(temp_db, project_id, "slot-project")
    storage = LocalSkillManager(temp_db)
    registry = create_skills_registry(temp_db, project_id=project_id)
    install = registry.get_tool("install_skill")
    assert install is not None
    skill_dir = _write_skill(tmp_path / "reinstall-skill", "reinstall-skill")

    token = set_project_context({"id": project_id})
    try:
        first = await install(source=str(skill_dir), project_scoped=True)
        assert first["success"] is True
        assert storage.delete_skill(first["skill_id"]) is True

        second = await install(source=str(skill_dir), project_scoped=True)
    finally:
        reset_project_context(token)

    assert second["success"] is True
    live = storage.get_by_name("reinstall-skill", project_id=project_id)
    assert live is not None
    assert live.id == second["skill_id"]


@pytest.mark.asyncio
async def test_soft_deleted_project_row_does_not_block_move_to_project(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_id = str(uuid.uuid4())
    _insert_project(temp_db, project_id, "slot-project")
    storage = LocalSkillManager(temp_db)
    registry = create_skills_registry(temp_db, project_id=project_id)
    install = registry.get_tool("install_skill")
    assert install is not None
    skill_dir = _write_skill(tmp_path / "move-skill", "move-skill")

    token = set_project_context({"id": project_id})
    try:
        stale = await install(source=str(skill_dir), project_scoped=True)
        assert stale["success"] is True
        assert storage.delete_skill(stale["skill_id"]) is True
    finally:
        reset_project_context(token)

    installed = await install(source=str(skill_dir), project_scoped=False)
    assert installed["success"] is True

    moved = storage.move_to_project(installed["skill_id"], project_id)

    assert moved.project_id == project_id
    live = storage.get_by_name("move-skill", project_id=project_id)
    assert live is not None
    assert live.id == installed["skill_id"]


@pytest.mark.asyncio
async def test_soft_deleted_installed_row_does_not_block_move_to_installed(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_id = str(uuid.uuid4())
    _insert_project(temp_db, project_id, "slot-project")
    storage = LocalSkillManager(temp_db)
    registry = create_skills_registry(temp_db, project_id=project_id)
    install = registry.get_tool("install_skill")
    assert install is not None
    skill_dir = _write_skill(tmp_path / "back-skill", "back-skill")

    stale = await install(source=str(skill_dir), project_scoped=False)
    assert stale["success"] is True
    assert storage.delete_skill(stale["skill_id"]) is True

    token = set_project_context({"id": project_id})
    try:
        scoped = await install(source=str(skill_dir), project_scoped=True)
    finally:
        reset_project_context(token)
    assert scoped["success"] is True

    moved = storage.move_to_installed(scoped["skill_id"])

    assert moved.project_id is None
    assert storage.get_by_name("back-skill", project_id=None) is not None
