"""Tests for SkillFile model and skill file CRUD operations."""

import hashlib
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.skills import LocalSkillManager, Skill, SkillFile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeRow(dict):
    """Dict subclass that supports dict[str, object]-style key access."""

    def keys(self) -> list[str]:
        return list(super().keys())


def _make_skill_file_row(**overrides: Any) -> FakeRow:
    """Build a FakeRow with sensible defaults for a SkillFile."""
    defaults: dict[str, Any] = {
        "id": "skf-abc123",
        "skill_id": "skl-parent1",
        "path": "references/api.md",
        "file_type": "reference",
        "content": "# API Reference",
        "content_hash": hashlib.sha256(b"# API Reference").hexdigest(),
        "size_bytes": 15,
        "deleted_at": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    defaults.update(overrides)
    return FakeRow(defaults)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(temp_db: HubDatabase) -> LocalSkillManager:
    return LocalSkillManager(temp_db)


@pytest.fixture
def sample_skill(storage: LocalSkillManager) -> Skill:
    return storage.create_skill(
        name="test-skill",
        description="Test skill",
        content="# Test",
    )


def _build_skill_files(skill_id: str) -> list[SkillFile]:
    """Return a small set of SkillFile objects for testing."""
    files = []
    for rel_path, ftype, body in [
        ("references/api.md", "reference", "# API docs"),
        ("scripts/build.sh", "script", "#!/bin/bash\necho hi"),
        ("LICENSE", "license", "MIT License"),
    ]:
        files.append(
            SkillFile(
                id="",  # set_skill_files generates IDs for new files
                skill_id=skill_id,
                path=rel_path,
                file_type=ftype,
                content=body,
                content_hash=_hash(body),
                size_bytes=len(body.encode()),
            )
        )
    return files


# ===========================================================================
# 1. SkillFile model tests
# ===========================================================================


class TestSkillFileModel:
    """Tests for the SkillFile dataclass."""

    def test_skill_file_from_row(self) -> None:
        row = _make_skill_file_row()
        sf = SkillFile.from_row(row)

        assert sf.id == "skf-abc123"
        assert sf.skill_id == "skl-parent1"
        assert sf.path == "references/api.md"
        assert sf.file_type == "reference"
        assert sf.content == "# API Reference"
        assert sf.size_bytes == 15
        assert sf.deleted_at is None

    def test_skill_file_to_dict_without_content(self) -> None:
        row = _make_skill_file_row()
        sf = SkillFile.from_row(row)
        d = sf.to_dict(include_content=False)

        assert "content" not in d
        assert d["path"] == "references/api.md"
        assert d["file_type"] == "reference"
        assert d["size_bytes"] == 15
        assert "content_hash" in d

    def test_skill_file_to_dict_with_content(self) -> None:
        row = _make_skill_file_row()
        sf = SkillFile.from_row(row)
        d = sf.to_dict(include_content=True)

        assert d["content"] == "# API Reference"
        assert d["path"] == "references/api.md"


# ===========================================================================
# 2. Storage CRUD tests
# ===========================================================================


class TestSkillFileCRUD:
    """Tests for skill file storage methods on LocalSkillManager."""

    def test_set_skill_files_creates_files(
        self, storage: LocalSkillManager, sample_skill: Skill
    ) -> None:
        files = _build_skill_files(sample_skill.id)
        changed = storage.set_skill_files(sample_skill.id, files)

        assert changed == 3
        stored = storage.get_skill_files(sample_skill.id, exclude_license=False)
        assert len(stored) == 3

    def test_set_skill_files_skips_unchanged(
        self, storage: LocalSkillManager, sample_skill: Skill
    ) -> None:
        files = _build_skill_files(sample_skill.id)
        storage.set_skill_files(sample_skill.id, files)

        # Second call with same content should change nothing
        changed = storage.set_skill_files(sample_skill.id, files)
        assert changed == 0

    def test_set_skill_files_updates_changed(
        self, storage: LocalSkillManager, sample_skill: Skill
    ) -> None:
        files = _build_skill_files(sample_skill.id)
        storage.set_skill_files(sample_skill.id, files)

        # Modify one file's content
        files[0].content = "# Updated API docs"
        files[0].content_hash = _hash("# Updated API docs")
        files[0].size_bytes = len(b"# Updated API docs")

        changed = storage.set_skill_files(sample_skill.id, files)
        assert changed == 1

    def test_set_skill_files_soft_deletes_orphans(
        self, storage: LocalSkillManager, sample_skill: Skill
    ) -> None:
        files = _build_skill_files(sample_skill.id)
        storage.set_skill_files(sample_skill.id, files)

        # Remove one file from the incoming set
        reduced = [f for f in files if f.path != "scripts/build.sh"]
        storage.set_skill_files(sample_skill.id, reduced)

        # The orphaned file should be soft-deleted, so not returned
        stored = storage.get_skill_files(sample_skill.id, exclude_license=False)
        paths = [f.path for f in stored]
        assert "scripts/build.sh" not in paths

    def test_get_skill_files_excludes_license_by_default(
        self, storage: LocalSkillManager, sample_skill: Skill
    ) -> None:
        files = _build_skill_files(sample_skill.id)
        storage.set_skill_files(sample_skill.id, files)

        stored = storage.get_skill_files(sample_skill.id)  # exclude_license=True by default
        paths = [f.path for f in stored]
        assert "LICENSE" not in paths
        assert len(stored) == 2

    def test_get_skill_files_includes_license_when_requested(
        self, storage: LocalSkillManager, sample_skill: Skill
    ) -> None:
        files = _build_skill_files(sample_skill.id)
        storage.set_skill_files(sample_skill.id, files)

        stored = storage.get_skill_files(sample_skill.id, exclude_license=False)
        paths = [f.path for f in stored]
        assert "LICENSE" in paths

    def test_get_skill_file_returns_content(
        self, storage: LocalSkillManager, sample_skill: Skill
    ) -> None:
        files = _build_skill_files(sample_skill.id)
        storage.set_skill_files(sample_skill.id, files)

        sf = storage.get_skill_file(sample_skill.id, "references/api.md")
        assert sf is not None
        assert sf.content == "# API docs"
        assert sf.file_type == "reference"

    def test_get_skill_file_not_found(
        self, storage: LocalSkillManager, sample_skill: Skill
    ) -> None:
        result = storage.get_skill_file(sample_skill.id, "nonexistent.md")
        assert result is None

    def test_delete_skill_files_cascade(
        self, storage: LocalSkillManager, sample_skill: Skill
    ) -> None:
        files = _build_skill_files(sample_skill.id)
        storage.set_skill_files(sample_skill.id, files)

        deleted = storage.delete_skill_files(sample_skill.id)
        assert deleted == 3

        stored = storage.get_skill_files(sample_skill.id, exclude_license=False)
        assert len(stored) == 0

    def test_restore_skill_files_cascade(
        self, storage: LocalSkillManager, sample_skill: Skill
    ) -> None:
        files = _build_skill_files(sample_skill.id)
        storage.set_skill_files(sample_skill.id, files)

        storage.delete_skill_files(sample_skill.id)
        restored = storage.restore_skill_files(sample_skill.id)
        assert restored == 3

        stored = storage.get_skill_files(sample_skill.id, exclude_license=False)
        assert len(stored) == 3

    def test_delete_skill_cascades_to_files(
        self, storage: LocalSkillManager, sample_skill: Skill
    ) -> None:
        files = _build_skill_files(sample_skill.id)
        storage.set_skill_files(sample_skill.id, files)

        storage.delete_skill(sample_skill.id)

        # Files should be soft-deleted along with the skill
        stored = storage.get_skill_files(sample_skill.id, exclude_license=False)
        assert len(stored) == 0

    def test_restore_skill_cascades_to_files(
        self, storage: LocalSkillManager, sample_skill: Skill
    ) -> None:
        files = _build_skill_files(sample_skill.id)
        storage.set_skill_files(sample_skill.id, files)

        storage.delete_skill(sample_skill.id)
        storage.restore(sample_skill.id)

        stored = storage.get_skill_files(sample_skill.id, exclude_license=False)
        assert len(stored) == 3
