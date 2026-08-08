"""Skill metadata publication and runtime validation tests."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.projects import LocalProjectManager
from gobby.storage.skills import DuplicateSkillError, LocalSkillManager, SkillFile


class RecordingNotifier:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, Any] | None]] = []

    def fire_change(
        self,
        event_type: str,
        skill_id: str,
        skill_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.events.append((event_type, skill_id, skill_name, metadata))


@pytest.fixture
def storage(temp_db: HubDatabase) -> LocalSkillManager:
    return LocalSkillManager(temp_db)


def _skill_file(skill_id: str = "") -> SkillFile:
    content = "console.log('ok')\n"
    return SkillFile(
        id="",
        skill_id=skill_id,
        path="scripts/run.js",
        file_type="script",
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        size_bytes=len(content.encode()),
    )


def _invalid_runtime_metadata() -> dict[str, object]:
    return {"gobby": {"runtime": {"node": "^22"}}}


def test_metadata_writes_reject_malformed_runtime(storage: LocalSkillManager) -> None:
    with pytest.raises(ValueError, match=r"runtime\.node"):
        storage.create_skill(
            name="invalid-create",
            description="Invalid create",
            content="# Invalid",
            metadata=_invalid_runtime_metadata(),
        )
    assert storage.get_by_name("invalid-create") is None

    skill = storage.create_skill(name="valid", description="Valid", content="# Valid")
    with pytest.raises(ValueError, match=r"runtime\.node"):
        storage.update_skill(skill.id, metadata=_invalid_runtime_metadata())
    with pytest.raises(ValueError, match=r"runtime\.node"):
        storage.update_skill_with_files(
            skill.id,
            description=skill.description,
            content=skill.content,
            version=skill.version,
            license=skill.license,
            compatibility=skill.compatibility,
            allowed_tools=skill.allowed_tools,
            metadata=_invalid_runtime_metadata(),
            files=[_skill_file(skill.id)],
        )

    with pytest.raises(ValueError, match=r"runtime\.node"):
        storage.create_skill_with_files(
            name="invalid-publication",
            description="Invalid publication",
            content="# Invalid",
            metadata=_invalid_runtime_metadata(),
            files=[_skill_file()],
        )
    assert storage.get_by_name("invalid-publication") is None
    file_count = storage.db.fetchone("SELECT COUNT(*) AS count FROM skill_files")
    assert file_count is not None
    assert file_count["count"] == 0


def test_create_skill_with_files_preserves_full_constructor_contract(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    notifier = RecordingNotifier()
    storage = LocalSkillManager(temp_db, notifier)
    project_id = (
        LocalProjectManager(temp_db)
        .create(
            name="skill-publication",
            repo_path=str(tmp_path),
        )
        .id
    )

    skill = storage.create_skill_with_files(
        name="contract-skill",
        description="Constructor contract",
        content="# Contract",
        version="1.2.3",
        license="MIT",
        compatibility="gobby >= 0.5",
        allowed_tools=["Bash"],
        metadata={"gobby": {"runtime": {"node": ">=22.18.0"}}},
        source_path=str(tmp_path / "contract-skill" / "SKILL.md"),
        source_type="filesystem",
        source_ref="main",
        enabled=False,
        always_apply=True,
        injection_format="full",
        project_id=project_id,
        source="installed",
        files=[_skill_file()],
    )

    namespace = uuid.uuid5(uuid.NAMESPACE_URL, "gobby:skills")
    assert skill.id == str(uuid.uuid5(namespace, f"contract-skill:{project_id}:project"))
    assert skill.source == "project"
    assert skill.project_id == project_id
    assert skill.enabled is False
    assert skill.always_apply is True
    assert skill.injection_format == "full"
    assert [item.path for item in storage.get_skill_files(skill.id)] == ["scripts/run.js"]
    assert notifier.events == [("create", skill.id, skill.name, None)]


def test_create_skill_with_files_rejects_project_bundled_template(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    storage = LocalSkillManager(temp_db)
    project_id = (
        LocalProjectManager(temp_db)
        .create(
            name="bundled-template-guard",
            repo_path=str(tmp_path),
        )
        .id
    )

    with pytest.raises(ValueError, match="bundled skill template"):
        storage.create_skill_with_files(
            name="invalid-project-template",
            description="Invalid project template",
            content="# Invalid",
            source_path="/tmp/gobby/install/shared/skills/example/SKILL.md",
            project_id=project_id,
            files=[],
        )

    assert (
        storage.get_by_name(
            "invalid-project-template",
            project_id=project_id,
            include_deleted=True,
        )
        is None
    )


def test_create_skill_with_files_uses_uuid4_on_deterministic_id_collision(
    storage: LocalSkillManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = storage.create_skill(
        name="collision-holder",
        description="Collision holder",
        content="# Existing",
    )
    monkeypatch.setattr(
        "gobby.storage.skills._metadata.uuid.uuid5",
        lambda *_args: uuid.UUID(existing.id),
    )

    created = storage.create_skill_with_files(
        name="collision-fallback",
        description="Collision fallback",
        content="# Fallback",
        files=None,
    )

    assert created.id != existing.id
    assert uuid.UUID(created.id).version == 4


def test_create_skill_with_files_translates_duplicate_scope(
    storage: LocalSkillManager,
) -> None:
    storage.create_skill_with_files(
        name="duplicate-scope",
        description="Original",
        content="# Original",
        files=[],
    )

    with pytest.raises(DuplicateSkillError, match="duplicate-scope"):
        storage.create_skill_with_files(
            name="duplicate-scope",
            description="Duplicate",
            content="# Duplicate",
            files=[],
        )


def test_create_skill_with_files_rolls_back_on_file_failure(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notifier = RecordingNotifier()
    storage = LocalSkillManager(temp_db, notifier)

    def fail_file_write(_conn: Transaction, _skill_id: str, _files: list[SkillFile]) -> int:
        raise RuntimeError("injected file write failure")

    monkeypatch.setattr(storage, "_set_skill_files", fail_file_write)

    with pytest.raises(RuntimeError, match="injected file write failure"):
        storage.create_skill_with_files(
            name="atomic-create",
            description="Atomic create",
            content="# Atomic",
            files=[_skill_file()],
        )

    assert storage.get_by_name("atomic-create") is None
    assert notifier.events == []


def test_restore_rolls_back_skill_and_files_on_failure(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notifier = RecordingNotifier()
    storage = LocalSkillManager(temp_db, notifier)
    skill = storage.create_skill_with_files(
        name="atomic-restore",
        description="Atomic restore",
        content="# Atomic",
        files=[_skill_file()],
    )
    storage.delete_skill(skill.id)
    notifier.events.clear()

    def fail_restore(_conn: Transaction, _skill_id: str) -> int:
        raise RuntimeError("injected restore failure")

    monkeypatch.setattr(storage, "_restore_skill_files", fail_restore, raising=False)

    with pytest.raises(RuntimeError, match="injected restore failure"):
        storage.restore(skill.id)

    stored = storage.get_by_name("atomic-restore", include_deleted=True)
    assert stored is not None
    assert stored.deleted_at is not None
    row = storage.db.fetchone(
        "SELECT COUNT(*) AS count FROM skill_files WHERE skill_id = %s AND deleted_at IS NOT NULL",
        (skill.id,),
    )
    assert row is not None
    assert row["count"] == 1
    assert notifier.events == []


def test_restore_never_exposes_active_skill_with_deleted_files(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notifier = RecordingNotifier()
    storage = LocalSkillManager(temp_db, notifier)
    skill = storage.create_skill_with_files(
        name="concurrent-restore",
        description="Concurrent restore",
        content="# Concurrent",
        files=[_skill_file()],
    )
    storage.delete_skill(skill.id)
    notifier.events.clear()
    row_updated = threading.Event()
    allow_file_restore = threading.Event()
    original_restore = storage._restore_skill_files

    def pause_before_file_restore(conn: Transaction, skill_id: str) -> int:
        row_updated.set()
        if not allow_file_restore.wait(timeout=5):
            raise TimeoutError("reader did not inspect the in-flight restore")
        return original_restore(conn, skill_id)

    monkeypatch.setattr(storage, "_restore_skill_files", pause_before_file_restore)

    with ThreadPoolExecutor(max_workers=1) as executor:
        restored = executor.submit(storage.restore, skill.id)
        assert row_updated.wait(timeout=5)
        try:
            visible = storage.get_by_name("concurrent-restore", include_deleted=True)
            assert visible is not None
            assert visible.deleted_at is not None
            assert storage.get_skill_files(skill.id) == []
            assert notifier.events == []
        finally:
            allow_file_restore.set()
        restored.result(timeout=5)

    visible = storage.get_by_name("concurrent-restore", include_deleted=True)
    assert visible is not None
    assert visible.deleted_at is None
    assert [item.path for item in storage.get_skill_files(skill.id)] == ["scripts/run.js"]
    assert notifier.events == [("create", skill.id, skill.name, None)]


@pytest.mark.parametrize(
    "imports",
    [
        "import gobby.storage.skills; import gobby.skills",
        "import gobby.skills; import gobby.storage.skills",
    ],
)
def test_runtime_validator_import_order_is_safe(imports: str) -> None:
    project_root = Path(__file__).resolve().parents[3]

    result = subprocess.run(
        [sys.executable, "-c", imports],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
