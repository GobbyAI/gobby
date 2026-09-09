from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from gobby.ask import snapshots as snapshot_module
from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.contracts import AskRequest, ProfileSnapshot
from gobby.ask.snapshots import AskSnapshotManager, SnapshotDriftError, SnapshotIndexRuntime
from gobby.ask.storage import AskRunStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.worktrees import LocalWorktreeManager

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
