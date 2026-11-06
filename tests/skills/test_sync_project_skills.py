"""Project-folder skills sync into project-scoped rows on daemon startup."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from gobby.skills.project_sync import sync_project_skills
from gobby.skills.sync import sync_bundled_skills
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.skills import LocalSkillManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit


def _write_skill(folder: Path, name: str, body: str = "Do the thing.") -> Path:
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill {name}.\nversion: 1.0.0\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return folder


def test_project_folder_skill_becomes_project_scoped_row_on_startup(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "game-goblins"
    project = isolated_checkout_factory(temp_db, "game-goblins", root=project_path).project
    _write_skill(project_path / ".gobby" / "skills" / "gusto", "gusto")

    result = sync_project_skills(temp_db)

    assert result["errors"] == []
    row = LocalSkillManager(temp_db).get_by_name(
        "gusto", project_id=project.id, include_global=False
    )
    assert row is not None
    assert row.project_id == project.id
    assert row.source == "project"
    assert row.source_path is not None
    assert Path(row.source_path).resolve().is_relative_to(project_path.resolve())


def test_edited_project_skill_updates_row_and_files_on_restart(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "game-goblins"
    project = isolated_checkout_factory(temp_db, "game-goblins", root=project_path).project
    skill_dir = _write_skill(project_path / ".gobby" / "skills" / "gusto", "gusto", "Old body.")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "tone.md").write_text("old tone\n", encoding="utf-8")
    sync_project_skills(temp_db)

    (skill_dir / "SKILL.md").write_text(
        "---\nname: gusto\ndescription: Test skill gusto.\nversion: 1.0.0\n---\n\nNew body.\n",
        encoding="utf-8",
    )
    (skill_dir / "references" / "tone.md").write_text("new tone\n", encoding="utf-8")

    result = sync_project_skills(temp_db)

    assert result["errors"] == []
    storage = LocalSkillManager(temp_db)
    row = storage.get_by_name("gusto", project_id=project.id, include_global=False)
    assert row is not None
    assert "New body." in row.content
    tone = storage.get_skill_file(row.id, "references/tone.md")
    assert tone is not None
    assert tone.content == "new tone\n"


def test_removed_project_folder_retires_only_the_sync_owned_row(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "game-goblins"
    project = isolated_checkout_factory(temp_db, "game-goblins", root=project_path).project
    skill_dir = _write_skill(project_path / ".gobby" / "skills" / "gusto", "gusto")
    sync_project_skills(temp_db)
    storage = LocalSkillManager(temp_db)
    storage.create_skill_with_files(
        name="ops-tool",
        description="Operator installed.",
        content="Operator body.",
        source_path=str(tmp_path / "elsewhere" / "ops-tool"),
        source_type="local",
        project_id=project.id,
        source="installed",
        files=None,
    )
    storage.create_skill_with_files(
        name="ext-tool",
        description="External source.",
        content="External body.",
        source_path="owner/ext-tool",
        source_type="github",
        project_id=project.id,
        source="installed",
        files=None,
    )
    shutil.rmtree(skill_dir)

    result = sync_project_skills(temp_db)

    assert result["errors"] == []
    assert result["orphaned"] == 1
    assert storage.get_by_name("gusto", project_id=project.id, include_global=False) is None
    assert storage.get_by_name("ops-tool", project_id=project.id, include_global=False)
    assert storage.get_by_name("ext-tool", project_id=project.id, include_global=False)


def test_project_folder_never_shadows_a_bundled_skill(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
) -> None:
    assert sync_bundled_skills(temp_db)["success"] is True
    project_path = tmp_path / "game-goblins"
    project = isolated_checkout_factory(temp_db, "game-goblins", root=project_path).project
    _write_skill(project_path / ".gobby" / "skills" / "gobby", "gobby", "Shadow body.")

    sync_project_skills(temp_db)

    storage = LocalSkillManager(temp_db)
    assert storage.get_by_name("gobby", project_id=project.id, include_global=False) is None
    resolved = storage.get_by_name("gobby", project_id=project.id)
    assert resolved is not None
    assert resolved.project_id is None


def test_project_folder_leaves_a_live_operator_row_of_the_same_name_untouched(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "game-goblins"
    project = isolated_checkout_factory(temp_db, "game-goblins", root=project_path).project
    _write_skill(project_path / ".gobby" / "skills" / "gusto", "gusto", "Folder body.")
    storage = LocalSkillManager(temp_db)
    operator = storage.create_skill_with_files(
        name="gusto",
        description="Operator installed.",
        content="Operator body.",
        source_path=str(tmp_path / "elsewhere" / "gusto"),
        source_type="local",
        project_id=project.id,
        source="project",
        files=None,
    )

    result = sync_project_skills(temp_db)

    assert result["errors"] == []
    assert result["warnings"]
    live = storage.get_by_name("gusto", project_id=project.id, include_global=False)
    assert live is not None
    assert live.id == operator.id
    assert live.content == "Operator body."
    assert "project_sync" not in (live.metadata or {})


def test_project_folder_does_not_adopt_a_soft_deleted_operator_row(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "game-goblins"
    project = isolated_checkout_factory(temp_db, "game-goblins", root=project_path).project
    _write_skill(project_path / ".gobby" / "skills" / "gusto", "gusto", "Folder body.")
    storage = LocalSkillManager(temp_db)
    operator = storage.create_skill_with_files(
        name="gusto",
        description="Operator installed.",
        content="Operator body.",
        source_path=str(tmp_path / "elsewhere" / "gusto"),
        source_type="local",
        project_id=project.id,
        source="project",
        files=None,
    )
    assert storage.delete_skill(operator.id) is True

    result = sync_project_skills(temp_db)

    assert result["errors"] == []
    live = storage.get_by_name("gusto", project_id=project.id, include_global=False)
    assert live is not None
    assert live.id != operator.id
    assert live.content == "Folder body."
    assert "project_sync" in (live.metadata or {})
