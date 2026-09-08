from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.snapshots import AskSnapshotManager, SnapshotDriftError, SnapshotIndexRuntime
from gobby.storage.hub.protocol import HubDatabase
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

    artifacts = AskArtifactStore(tmp_path / "state", project_id, "run-1")
    manager = AskSnapshotManager(
        worktree_storage=LocalWorktreeManager(temp_db),
        index_preparer=prepare_index,
    )
    deadline = datetime.now(UTC) + timedelta(minutes=10)
    snapshot = manager.prepare(
        run_id="run-1",
        project_id=project_id,
        repository_root=repo,
        commit_oid=historical,
        deadline_at=deadline,
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

    relative_path = snapshot.manifest_pointer["relative_path"]
    assert isinstance(relative_path, str)
    snapshot_body_path = artifacts.run_root / relative_path
    snapshot_body = snapshot_body_path.read_bytes()
    snapshot_body_path.write_bytes(b"{}")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        manager.recover(snapshot.manifest_pointer, deadline_at=deadline, artifacts=artifacts)
    snapshot_body_path.write_bytes(snapshot_body)

    (snapshot.source_root / "source.py").write_text("VERSION = 'tampered'\n", encoding="utf-8")
    with pytest.raises(SnapshotDriftError, match="working tree drift"):
        manager.recover(snapshot.manifest_pointer, deadline_at=deadline, artifacts=artifacts)
    _git(snapshot.source_root, "reset", "--hard", "--quiet", historical)

    shutil.rmtree(snapshot.source_root)
    recovered = manager.recover(
        snapshot.manifest_pointer, deadline_at=deadline, artifacts=artifacts
    )
    assert recovered.commit_oid == historical
    assert (recovered.source_root / "source.py").read_text(encoding="utf-8") == "VERSION = 'old'\n"
    assert len(prepared) == 2

    manager.release(recovered, artifacts=artifacts)
    assert not recovered.source_root.exists()
    assert manager.worktree_storage.get(recovered.worktree_id) is None
    assert artifacts.manifest_path.exists()
    assert artifacts.read_body(recovered.manifest_pointer)["commit_oid"] == historical
    assert _git(repo, "status", "--porcelain=v1") == caller_status
