from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from gobby.ask import snapshots as snapshot_module
from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.contracts import AskRequest, ProfileSnapshot, SnapshotGeneration
from gobby.ask.snapshots import (
    AskSnapshotManager,
    SnapshotCleanupError,
    SnapshotDriftError,
    SnapshotIndexRuntime,
)
from gobby.ask.storage import AskRunStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.managed_credentials import ManagedCredentialManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.machine_id import require_machine_id

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Ask Test",
        "-c",
        "user.email=ask@example.invalid",
        "commit",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD")


def _profile(identifier: str, _timeout: float) -> ProfileSnapshot:
    return ProfileSnapshot(
        identifier=identifier,
        definition_id=f"definition-{identifier}",
        definition_updated_at="2026-09-08T12:00:00+00:00",
        effective={"name": identifier, "provider": "codex", "model": "gpt-test"},
    )


def _run_storage(
    temp_db: HubDatabase,
    project_id: str,
    repo: Path,
    commit_oid: str,
    *,
    timeout_seconds: float = 30,
) -> tuple[AskRunStorage, str]:
    storage = AskRunStorage(
        LocalPipelineExecutionManager(temp_db, project_id=project_id),
        profile_resolver=_profile,
    )
    record = storage.start(
        AskRequest(
            question="What is pinned?",
            project_id=project_id,
            commit_ref=commit_oid,
            investigator_profile="investigator",
            reviewer_profile="reviewer",
            timeout_seconds=timeout_seconds,
        ),
        repo,
    )
    caller_session = SessionManager(temp_db).register(
        external_id=f"ask-snapshot-{record.run_id}",
        machine_id=require_machine_id(),
        source="codex",
        project_id=project_id,
    )
    storage.bind_execution_context(
        record.run_id,
        project_root=repo,
        caller_session_id=caller_session.id,
    )
    return storage, record.run_id


def _branch_gcode() -> Path:
    executable = Path(__file__).parents[2] / "target" / "debug" / "gcode"
    assert executable.is_file(), "build the branch-local gcode binary before snapshot tests"
    return executable


def test_historical_snapshot_isolation_and_recovery(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    project_id = str(sample_project["id"])
    repo = tmp_path / "caller"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    (repo / ".gobby").mkdir()
    (repo / ".gobby" / "project.json").write_text(
        json.dumps({"id": project_id, "name": "ask-test"}), encoding="utf-8"
    )
    (repo / "source.py").write_text("VERSION = 'old'\n", encoding="utf-8")
    historical = _commit(repo, "historical")
    (repo / "source.py").write_text("VERSION = 'new'\n", encoding="utf-8")
    _commit(repo, "current")
    (repo / "source.py").write_text("VERSION = 'dirty'\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("caller only\n", encoding="utf-8")
    caller_head = _git(repo, "rev-parse", "HEAD")
    caller_status = _git(repo, "status", "--porcelain=v1")

    prepared: list[Path] = []

    async def prepare_index(path: Path, _deadline: datetime) -> SnapshotIndexRuntime:
        prepared.append(path)
        return SnapshotIndexRuntime(
            executable=Path("/branch/target/debug/gcode"),
            env={"GOBBY_MANAGED_EXECUTION_BOOTSTRAP": "/redacted/grant.json"},
            managed_execution_id="managed-index",
            credential_generation=1,
        )

    storage, run_id = _run_storage(temp_db, project_id, repo, historical)
    artifacts = AskArtifactStore(tmp_path / "state", project_id, run_id)
    manager = AskSnapshotManager(
        worktree_storage=LocalWorktreeManager(temp_db),
        index_preparer=prepare_index,
        index_releaser=lambda _runtime: None,
        run_storage=storage,
        snapshot_executable=_branch_gcode(),
    )
    snapshot = manager.prepare(
        run_id=run_id,
        repository_root=repo,
        artifacts=artifacts,
    )

    assert (snapshot.source_root / "source.py").read_text(encoding="utf-8") == "VERSION = 'old'\n"
    assert snapshot.commit_oid == historical
    assert snapshot.inventory["complete"] is True
    assert {entry["path"] for entry in snapshot.inventory["entries"]} == {
        ".gobby/project.json",
        "source.py",
    }
    assert prepared == [snapshot.source_root]
    assert snapshot.binding["inventory_digest"] == snapshot.inventory["digest"]
    managed = manager.worktree_storage.get(snapshot.worktree_id)
    assert managed is not None
    assert managed.workspace_role == "ask_snapshot"
    assert managed.agent_session_id == storage.execution_inputs(run_id)["caller_session_id"]
    assert _git(repo, "rev-parse", "HEAD") == caller_head
    assert _git(repo, "status", "--porcelain=v1") == caller_status

    lifecycle_body_path = artifacts.run_root / snapshot.manifest_pointer["relative_path"]
    lifecycle_body = lifecycle_body_path.read_bytes()
    lifecycle_body_path.write_bytes(b"{}")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        manager.recover(run_id=run_id, artifacts=artifacts)
    lifecycle_body_path.write_bytes(lifecycle_body)

    (snapshot.source_root / "source.py").write_text("VERSION = 'tampered'\n", encoding="utf-8")
    with pytest.raises(SnapshotDriftError, match="materialized content"):
        manager.recover(run_id=run_id, artifacts=artifacts)
    (snapshot.source_root / "source.py").write_text("VERSION = 'old'\n", encoding="utf-8")

    unexpected = snapshot.source_root / "injected.py"
    unexpected.write_text("SECRET = 'untracked'\n", encoding="utf-8")
    with pytest.raises(SnapshotDriftError, match="unexpected materialized path"):
        manager.recover(run_id=run_id, artifacts=artifacts)
    unexpected.unlink()

    shutil.rmtree(snapshot.source_root)
    recovered = manager.recover(run_id=run_id, artifacts=artifacts)
    assert recovered.commit_oid == historical
    assert (recovered.source_root / "source.py").read_text(encoding="utf-8") == "VERSION = 'old'\n"
    assert len(prepared) == 2

    manager.release(recovered, artifacts=artifacts)
    assert not recovered.source_root.exists()
    assert manager.worktree_storage.get(recovered.worktree_id) is None
    assert artifacts.manifest_path.exists()
    lifecycle = artifacts.read_body(recovered.manifest_pointer)
    identity = artifacts.read_body(lifecycle["snapshot_artifact"])
    assert identity["commit_oid"] == historical
    assert _git(repo, "status", "--porcelain=v1") == caller_status


def test_native_snapshot_lifecycle_generations_and_fault_cleanup(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = str(sample_project["id"])
    repo = tmp_path / "caller"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    (repo / "source.py").write_text("VALUE = 'pinned'\n", encoding="utf-8")
    commit_oid = _commit(repo, "pinned")
    storage, run_id = _run_storage(temp_db, project_id, repo, commit_oid)
    artifacts = AskArtifactStore(tmp_path / "state", project_id, run_id)
    prepared_count = 0
    released: list[str] = []

    async def prepare_index(path: Path, _deadline: datetime) -> SnapshotIndexRuntime:
        nonlocal prepared_count
        prepared_count += 1
        return SnapshotIndexRuntime(
            executable=_branch_gcode(),
            env={},
            managed_execution_id=f"managed-{prepared_count}",
            credential_generation=prepared_count,
        )

    manager = AskSnapshotManager(
        worktree_storage=LocalWorktreeManager(temp_db),
        index_preparer=prepare_index,
        index_releaser=lambda runtime: released.append(runtime.managed_execution_id),
        run_storage=storage,
        snapshot_executable=_branch_gcode(),
    )
    first = manager.prepare(run_id=run_id, repository_root=repo, artifacts=artifacts)
    assert first.generation == 1
    assert storage.get_snapshot_generation(run_id) is not None

    real_remaining = snapshot_module._remaining_seconds

    def expired(_deadline: datetime) -> float:
        raise TimeoutError("Ask snapshot deadline exceeded")

    monkeypatch.setattr(snapshot_module, "_remaining_seconds", expired)
    with pytest.raises(TimeoutError, match="deadline exceeded"):
        manager.recover(run_id=run_id, artifacts=artifacts)
    assert prepared_count == 1
    assert first.source_root.exists()
    monkeypatch.setattr(snapshot_module, "_remaining_seconds", real_remaining)

    second = manager.recover(run_id=run_id, artifacts=artifacts)
    assert second.generation == 2
    assert released == ["managed-1"]
    shutil.rmtree(second.source_root)

    real_publish = storage.publish_snapshot_generation

    def fail_publish(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected lifecycle publication failure")

    monkeypatch.setattr(storage, "publish_snapshot_generation", fail_publish)
    with pytest.raises(RuntimeError, match="publication failure"):
        manager.recover(run_id=run_id, artifacts=artifacts)
    assert not second.source_root.exists()
    assert manager.worktree_storage.get_by_path(str(second.source_root)) is None
    assert released[-1] == "managed-3"
    current = storage.get_snapshot_generation(run_id)
    assert current is not None
    assert current.generation == 2

    monkeypatch.setattr(storage, "publish_snapshot_generation", real_publish)
    third = manager.recover(run_id=run_id, artifacts=artifacts)
    assert third.generation == 3
    assert third.worktree_id != second.worktree_id
    artifacts.verify_manifest()
    lifecycle_pointers = [
        item
        for item in json.loads(artifacts.manifest_path.read_text())["artifacts"]
        if item["kind"] == "snapshot-lifecycle"
    ]
    assert len(lifecycle_pointers) == 4
    assert all(artifacts.read_body(pointer)["generation"] >= 1 for pointer in lifecycle_pointers)

    retained = artifacts.write_body("evidence-result", {"schema_version": 1, "canary": True})
    retained_path = artifacts.run_root / retained["relative_path"]
    retained_bytes = retained_path.read_bytes()
    retained_path.write_bytes(b"{}")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        manager.recover(run_id=run_id, artifacts=artifacts)
    retained_path.write_bytes(retained_bytes)

    manager.release(third, artifacts=artifacts)
    assert released[-1] == "managed-4"
    assert not third.source_root.exists()


@pytest.mark.asyncio
async def test_snapshot_metadata_preparation_obeys_remaining_deadline(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "caller"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    (repo / "source.py").write_text("VALUE = 'pinned'\n", encoding="utf-8")
    commit_oid = _commit(repo, "pinned")
    storage, run_id = _run_storage(temp_db, str(sample_project["id"]), repo, commit_oid)
    record = storage.get(run_id)
    assert record is not None

    async def unexpected_index(_path: Path, _deadline: datetime) -> SnapshotIndexRuntime:
        raise AssertionError("materialization must not start index preparation")

    manager = AskSnapshotManager(
        worktree_storage=LocalWorktreeManager(temp_db),
        run_storage=storage,
        snapshot_executable=_branch_gcode(),
        index_preparer=unexpected_index,
        index_releaser=lambda _runtime: None,
    )
    metadata_started = asyncio.Event()
    metadata_cancelled = asyncio.Event()

    async def stalled_metadata(_repository_root: Path, _source_root: Path) -> None:
        metadata_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            metadata_cancelled.set()

    def materialized_source(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(snapshot_module, "_native_snapshot", materialized_source)
    monkeypatch.setattr(snapshot_module, "ensure_project_json_for_isolation", stalled_metadata)
    watchdog = asyncio.timeout(2)
    with pytest.raises(TimeoutError):
        async with watchdog:
            await manager._materialize(
                record=record,
                repository_root=repo,
                source_root=tmp_path / "snapshot",
                deadline_at=datetime.now(UTC) + timedelta(seconds=0.1),
            )
    assert metadata_started.is_set()
    assert metadata_cancelled.is_set()
    assert not watchdog.expired(), "the Ask deadline must cancel metadata before the test watchdog"


def test_snapshot_creation_and_index_faults_leave_no_worktree(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = str(sample_project["id"])
    repo = tmp_path / "caller"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    (repo / "source.py").write_text("VALUE = 'pinned'\n", encoding="utf-8")
    commit_oid = _commit(repo, "pinned")
    worktrees = LocalWorktreeManager(temp_db)
    real_create = worktrees.create

    storage, run_id = _run_storage(temp_db, project_id, repo, commit_oid)
    artifacts = AskArtifactStore(tmp_path / "create-state", project_id, run_id)

    def fail_create(**_kwargs: object) -> object:
        raise RuntimeError("injected worktree row failure")

    async def unexpected_index(_path: Path, _deadline: datetime) -> SnapshotIndexRuntime:
        raise AssertionError("index preparation must not run after worktree creation fails")

    monkeypatch.setattr(worktrees, "create", fail_create)
    manager = AskSnapshotManager(
        worktree_storage=worktrees,
        index_preparer=unexpected_index,
        index_releaser=lambda _runtime: None,
        run_storage=storage,
        snapshot_executable=_branch_gcode(),
    )
    with pytest.raises(RuntimeError, match="worktree row failure"):
        manager.prepare(run_id=run_id, repository_root=repo, artifacts=artifacts)
    source_root = artifacts.run_root / "source"
    assert not source_root.exists()
    assert worktrees.get_by_path(str(source_root)) is None

    monkeypatch.setattr(worktrees, "create", real_create)
    storage, run_id = _run_storage(temp_db, project_id, repo, commit_oid)
    artifacts = AskArtifactStore(tmp_path / "index-state", project_id, run_id)

    async def fail_index(_path: Path, _deadline: datetime) -> SnapshotIndexRuntime:
        raise RuntimeError("injected index failure")

    manager = AskSnapshotManager(
        worktree_storage=worktrees,
        index_preparer=fail_index,
        index_releaser=lambda _runtime: None,
        run_storage=storage,
        snapshot_executable=_branch_gcode(),
    )
    with pytest.raises(RuntimeError, match="index failure"):
        manager.prepare(run_id=run_id, repository_root=repo, artifacts=artifacts)
    source_root = artifacts.run_root / "source"
    assert not source_root.exists()
    assert worktrees.get_by_path(str(source_root)) is None


@pytest.mark.asyncio
async def test_snapshot_cancellation_waits_for_worktree_creation_before_cleanup(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = str(sample_project["id"])
    repo = tmp_path / "caller"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    (repo / "source.py").write_text("VALUE = 'pinned'\n", encoding="utf-8")
    commit_oid = _commit(repo, "pinned")
    storage, run_id = _run_storage(temp_db, project_id, repo, commit_oid)
    artifacts = AskArtifactStore(tmp_path / "cancel-worktree-state", project_id, run_id)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    real_git = snapshot_module._git

    def slow_git(
        root: Path,
        *arguments: str,
        timeout: float,
        input_bytes: bytes | None = None,
    ) -> bytes:
        if arguments[:2] == ("worktree", "add"):
            entered.set()
            assert release.wait(timeout=2)
            try:
                return real_git(
                    root,
                    *arguments,
                    timeout=timeout,
                    input_bytes=input_bytes,
                )
            finally:
                finished.set()
        return real_git(root, *arguments, timeout=timeout, input_bytes=input_bytes)

    async def unexpected_index(_path: Path, _deadline: datetime) -> SnapshotIndexRuntime:
        raise AssertionError("cancelled worktree creation must not prepare an index")

    monkeypatch.setattr(snapshot_module, "_git", slow_git)
    manager = AskSnapshotManager(
        worktree_storage=LocalWorktreeManager(temp_db),
        index_preparer=unexpected_index,
        index_releaser=lambda _runtime: None,
        run_storage=storage,
        snapshot_executable=_branch_gcode(),
    )
    task = asyncio.create_task(
        manager.prepare_async(run_id=run_id, repository_root=repo, artifacts=artifacts)
    )
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(finished.wait, 2)
    source_root = artifacts.run_root / "source"
    assert not source_root.exists()
    assert manager.worktree_storage.get_by_path(str(source_root)) is None


@pytest.mark.asyncio
async def test_snapshot_cancellation_revokes_grant_issued_in_blocking_call(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    project_id = str(sample_project["id"])
    repo = tmp_path / "caller"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    (repo / "source.py").write_text("VALUE = 'pinned'\n", encoding="utf-8")
    commit_oid = _commit(repo, "pinned")
    storage, run_id = _run_storage(temp_db, project_id, repo, commit_oid)
    source_root = tmp_path / "source"
    source_root.mkdir()
    spoofed_session_id = uuid4()
    execution_id = uuid4()
    entered = threading.Event()
    release = threading.Event()
    revoked: list[tuple[UUID, int | None, str]] = []
    issued_for: list[UUID] = []

    class BlockingCredentialManager:
        def issue_tool_request(self, **kwargs: object) -> SimpleNamespace:
            issued_for.append(cast("UUID", kwargs["session_id"]))
            entered.set()
            assert release.wait(timeout=2)
            credential = SimpleNamespace(
                managed_execution_id=execution_id,
                credential_generation=1,
            )
            return SimpleNamespace(
                project_path=str(source_root),
                project_id=project_id,
                credential=credential,
            )

        def revoke(
            self,
            managed_execution_id: UUID,
            *,
            generation: int | None = None,
            reason: str,
        ) -> None:
            revoked.append((managed_execution_id, generation, reason))

    manager = AskSnapshotManager(
        worktree_storage=LocalWorktreeManager(temp_db),
        run_storage=storage,
        credential_manager=cast("ManagedCredentialManager", BlockingCredentialManager()),
        snapshot_executable=_branch_gcode(),
    )
    task = asyncio.create_task(
        manager._prepare_index(
            run_id, source_root, datetime.now(UTC) + timedelta(seconds=10), commit_oid
        )
    )
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    original_caller = UUID(str(storage.execution_inputs(run_id)["caller_session_id"]))
    assert issued_for == [original_caller]
    assert original_caller != spoofed_session_id
    assert revoked == [(execution_id, 1, "ask_snapshot_preparation_failed")]


@pytest.mark.asyncio
async def test_snapshot_cancellation_after_generation_commit_preserves_authority(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = str(sample_project["id"])
    repo = tmp_path / "caller"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    (repo / "source.py").write_text("VALUE = 'pinned'\n", encoding="utf-8")
    commit_oid = _commit(repo, "pinned")
    storage, run_id = _run_storage(temp_db, project_id, repo, commit_oid)
    artifacts = AskArtifactStore(tmp_path / "commit-cancel-state", project_id, run_id)
    prepared_count = 0
    released: list[str] = []

    async def prepare_index(_path: Path, _deadline: datetime) -> SnapshotIndexRuntime:
        nonlocal prepared_count
        prepared_count += 1
        return SnapshotIndexRuntime(
            executable=_branch_gcode(),
            env={},
            managed_execution_id=f"managed-{prepared_count}",
            credential_generation=prepared_count,
        )

    manager = AskSnapshotManager(
        worktree_storage=LocalWorktreeManager(temp_db),
        index_preparer=prepare_index,
        index_releaser=lambda runtime: released.append(runtime.managed_execution_id),
        run_storage=storage,
        snapshot_executable=_branch_gcode(),
    )
    real_publish = storage.publish_snapshot_generation
    published = threading.Event()
    release = threading.Event()

    def publish_then_pause(
        target_run_id: str,
        *,
        generation: int,
        lifecycle_artifact: dict[str, Any],
        expected_previous_generation: int | None,
        deadline_at: datetime,
    ) -> SnapshotGeneration:
        result = real_publish(
            target_run_id,
            generation=generation,
            lifecycle_artifact=lifecycle_artifact,
            expected_previous_generation=expected_previous_generation,
            deadline_at=deadline_at,
        )
        published.set()
        assert release.wait(timeout=2)
        return result

    monkeypatch.setattr(storage, "publish_snapshot_generation", publish_then_pause)
    task = asyncio.create_task(
        manager.prepare_async(run_id=run_id, repository_root=repo, artifacts=artifacts)
    )
    assert await asyncio.to_thread(published.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    current = storage.get_snapshot_generation(run_id)
    assert current is not None
    assert current.generation == 1
    source_root = artifacts.run_root / "source"
    assert source_root.exists()
    assert manager.worktree_storage.get_by_path(str(source_root)) is not None
    assert released == []

    monkeypatch.setattr(storage, "publish_snapshot_generation", real_publish)
    recovered = await manager.recover_async(run_id=run_id, artifacts=artifacts)
    assert recovered.generation == 2
    assert released == ["managed-1"]
    manager.release(recovered, artifacts=artifacts)


def test_snapshot_failed_git_cleanup_retains_worktree_owner(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = str(sample_project["id"])
    repo = tmp_path / "caller"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    (repo / "source.py").write_text("VALUE = 'pinned'\n", encoding="utf-8")
    commit_oid = _commit(repo, "pinned")
    storage, run_id = _run_storage(temp_db, project_id, repo, commit_oid, timeout_seconds=120)
    artifacts = AskArtifactStore(tmp_path / "cleanup-state", project_id, run_id)

    async def prepare_index(_path: Path, _deadline: datetime) -> SnapshotIndexRuntime:
        return SnapshotIndexRuntime(
            executable=_branch_gcode(),
            env={},
            managed_execution_id="managed-cleanup",
            credential_generation=1,
        )

    manager = AskSnapshotManager(
        worktree_storage=LocalWorktreeManager(temp_db),
        index_preparer=prepare_index,
        index_releaser=lambda _runtime: None,
        run_storage=storage,
        snapshot_executable=_branch_gcode(),
    )
    snapshot = manager.prepare(run_id=run_id, repository_root=repo, artifacts=artifacts)
    real_git = snapshot_module._git
    observed_timeouts: list[float] = []

    def fail_cleanup(
        root: Path,
        *arguments: str,
        timeout: float,
        input_bytes: bytes | None = None,
    ) -> bytes:
        observed_timeouts.append(timeout)
        if arguments[:2] in {("worktree", "remove"), ("worktree", "prune")}:
            raise subprocess.TimeoutExpired(["git", *arguments], timeout)
        return real_git(root, *arguments, timeout=timeout, input_bytes=input_bytes)

    monkeypatch.setattr(snapshot_module, "_git", fail_cleanup)
    with pytest.raises(SnapshotCleanupError, match="ownership retained"):
        manager.release(snapshot, artifacts=artifacts)
    assert snapshot.source_root.exists()
    assert manager.worktree_storage.get(snapshot.worktree_id) is not None
    assert len(observed_timeouts) == 2
    assert 0 < observed_timeouts[1] <= observed_timeouts[0] <= 10

    monkeypatch.setattr(snapshot_module, "_git", real_git)
    manager._delete_worktree(
        snapshot.repository_root,
        snapshot.source_root,
        snapshot.worktree_id,
    )
    assert manager.worktree_storage.get(snapshot.worktree_id) is None


def test_hostile_git_environment_filters_and_hooks_cannot_change_snapshot(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = str(sample_project["id"])
    repo = tmp_path / "caller"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    (repo / ".gitattributes").write_text("source.py filter=attack\n", encoding="utf-8")
    (repo / "source.py").write_text("VALUE = 'trusted'\n", encoding="utf-8")
    original = _commit(repo, "trusted")
    (repo / "source.py").write_text("VALUE = 'replacement'\n", encoding="utf-8")
    replacement = _commit(repo, "replacement")
    _git(repo, "replace", original, replacement)
    hook_marker = tmp_path / "hook-ran"
    filter_marker = tmp_path / "filter-ran"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    post_checkout = hooks / "post-checkout"
    post_checkout.write_text(f"#!/bin/sh\ntouch {hook_marker}\n", encoding="utf-8")
    post_checkout.chmod(0o755)
    _git(repo, "config", "core.hooksPath", str(hooks))
    _git(repo, "config", "filter.attack.smudge", f"sh -c 'touch {filter_marker}; cat'")
    _git(repo, "config", "filter.attack.required", "true")

    contaminant = tmp_path / "contaminant"
    contaminant.mkdir()
    _git(contaminant, "init", "--quiet", "-b", "main")
    monkeypatch.setenv("GIT_DIR", str(contaminant / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(contaminant))
    storage, run_id = _run_storage(temp_db, project_id, repo, original)
    artifacts = AskArtifactStore(tmp_path / "state", project_id, run_id)

    async def prepare_index(_path: Path, _deadline: datetime) -> SnapshotIndexRuntime:
        return SnapshotIndexRuntime(
            executable=_branch_gcode(),
            env={},
            managed_execution_id="managed-hostile",
            credential_generation=1,
        )

    manager = AskSnapshotManager(
        worktree_storage=LocalWorktreeManager(temp_db),
        index_preparer=prepare_index,
        index_releaser=lambda _runtime: None,
        run_storage=storage,
        snapshot_executable=_branch_gcode(),
    )
    snapshot = manager.prepare(run_id=run_id, repository_root=repo, artifacts=artifacts)
    assert snapshot.commit_oid == original
    assert (snapshot.source_root / "source.py").read_text() == "VALUE = 'trusted'\n"
    assert not hook_marker.exists()
    assert not filter_marker.exists()
    manager.release(snapshot, artifacts=artifacts)
