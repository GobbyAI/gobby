"""Skill installation and refresh sanitize before publishing stored instructions."""

import hashlib
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.skills import create_skills_registry
from gobby.skills.hubs.base import DownloadResult
from gobby.skills.hubs.manager import HubManager
from gobby.skills.updater import SkillUpdater
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.skills import LocalSkillManager

pytestmark = pytest.mark.unit


@pytest.fixture
def hidden_skill_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "sample"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: sample\ndescription: Sample instructions\n---\nbody\u202e\U000e0061\n",
        encoding="utf-8",
    )
    references = directory / "references"
    references.mkdir()
    (references / "guide.md").write_text("reference\u202e\U000e0061", encoding="utf-8")
    return directory


def assert_clean_publication(storage: LocalSkillManager, skill_id: str) -> None:
    skill = storage.get_skill(skill_id)
    assert skill.content == "body"
    files = storage.get_skill_files(skill_id, include_content=True)
    assert len(files) == 1
    assert files[0].content == "reference"
    assert files[0].content_hash == hashlib.sha256(b"reference").hexdigest()
    assert files[0].size_bytes == len(b"reference")


@pytest.mark.asyncio
async def test_hub_tool_install_stores_clean_content(
    temp_db: HubDatabase, hidden_skill_dir: Path
) -> None:
    hub_manager = MagicMock(spec=HubManager)
    provider = MagicMock()
    provider.download_skill = AsyncMock(
        return_value=DownloadResult(success=True, slug="sample", path=str(hidden_skill_dir))
    )
    hub_manager.get_provider.return_value = provider
    hub_manager.has_hub.return_value = True
    registry = create_skills_registry(temp_db, hub_manager=hub_manager)
    result = await registry.call("install_skill", {"source": "testhub:sample"})
    assert result["success"] is True, result
    assert_clean_publication(LocalSkillManager(temp_db), result["skill_id"])


@pytest.mark.asyncio
async def test_zip_tool_install_stores_clean_content(
    temp_db: HubDatabase, hidden_skill_dir: Path, tmp_path: Path
) -> None:
    archive = tmp_path / "sample.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in hidden_skill_dir.rglob("*"):
            if path.is_file():
                bundle.write(path, path.relative_to(hidden_skill_dir.parent))
    registry = create_skills_registry(temp_db)
    result = await registry.call("install_skill", {"source": str(archive)})
    assert result["success"] is True, result
    assert result["source_type"] == "zip"
    assert_clean_publication(LocalSkillManager(temp_db), result["skill_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("use_async", [False, True])
async def test_github_refresh_stores_clean_content(
    temp_db: HubDatabase,
    hidden_skill_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_async: bool,
) -> None:
    storage = LocalSkillManager(temp_db)
    skill = storage.create_skill(
        name="sample",
        description="Before",
        content="before",
        source_type="github",
        source_path="github:owner/repo",
        source_ref="main",
    )
    monkeypatch.setattr(
        "gobby.skills.updater.clone_skill_repo", MagicMock(return_value=hidden_skill_dir)
    )
    monkeypatch.setattr(
        "gobby.skills.updater.clone_skill_repo_async", AsyncMock(return_value=hidden_skill_dir)
    )
    updater = SkillUpdater(storage)
    result = (
        await updater.update_skill_async(skill.id) if use_async else updater.update_skill(skill.id)
    )
    assert result.success, result.error
    assert result.updated
    assert_clean_publication(storage, skill.id)
