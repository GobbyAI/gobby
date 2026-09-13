from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from gobby.agents.code_index import CodeIndexPreflightResult
from gobby.ask import snapshots as snapshot_module
from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.contracts import AskRequest, ProfileSnapshot, SnapshotGeneration
from gobby.ask.snapshots import AskSnapshotManager, SnapshotIndexRuntime
from gobby.ask.storage import AskRunStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.managed_credential_types import ManagedCredential, ManagedToolCredential
from gobby.storage.managed_credentials import ManagedCredentialManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
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
    *,
    timeout_seconds: float = 30,
) -> tuple[AskRunStorage, str]:
    project_json = repo / ".gobby" / "project.json"
    if not project_json.exists():
        project_json.parent.mkdir(exist_ok=True)
        project_json.write_text(json.dumps({"id": project_id, "name": "ask-test"}))
    storage = AskRunStorage(
        LocalPipelineExecutionManager(temp_db, project_id=project_id),
        profile_resolver=_profile,
    )
    record = storage.start(
        AskRequest(
            question="What is pinned?",
            project_id=project_id,
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


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "caller"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    (repo / "source.py").write_text("VALUE = 'indexed'\n")
    _commit(repo, "initial")
    return repo


async def test_bind_uses_caller_index_and_fences_recovery(
    temp_db: HubDatabase, sample_project: dict[str, object], tmp_path: Path
) -> None:
    repo = _repository(tmp_path)
    project_id = str(sample_project["id"])
    storage, run_id = _run_storage(temp_db, project_id, repo)
    artifacts = AskArtifactStore(tmp_path / "state", project_id, run_id)
    calls: list[Path] = []

    async def probe(root: Path, deadline: datetime) -> SnapshotIndexRuntime:
        assert deadline > datetime.now(UTC)
        calls.append(root)
        return SnapshotIndexRuntime(Path("/usr/bin/true"), {}, str(uuid4()), len(calls))

    manager = AskSnapshotManager(
        run_storage=storage, index_preparer=probe, snapshot_executable=Path("/usr/bin/true")
    )
    before = _git(repo, "worktree", "list", "--porcelain")
    prepared = await manager.prepare_async(run_id=run_id, repository_root=repo, artifacts=artifacts)
    assert prepared.binding["project_id"] == project_id
    assert prepared.source_root == repo.resolve()
    assert prepared.binding["commit_oid"] == _git(repo, "rev-parse", "HEAD")
    assert not (artifacts.run_root / "source").exists()
    recovered = await manager.recover_async(run_id=run_id, artifacts=artifacts)
    assert calls == [repo.resolve(), repo.resolve()]
    assert recovered.generation == prepared.generation + 1
    with pytest.raises(ValueError, match="generation"):
        storage.publish_snapshot_generation(
            run_id,
            generation=2,
            lifecycle_artifact=prepared.manifest_pointer,
            expected_previous_generation=1,
            deadline_at=datetime.now(UTC) + timedelta(seconds=30),
        )
    with pytest.raises(RuntimeError, match="generation"):
        manager.release(prepared, artifacts=artifacts)
    manager.release(recovered, artifacts=artifacts)
    assert _git(repo, "worktree", "list", "--porcelain") == before
    assert (repo / "source.py").read_text() == "VALUE = 'indexed'\n"


async def test_bind_grant_and_bounded_probes_use_real_repository(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repository(tmp_path)
    project_id = str(sample_project["id"])
    storage, run_id = _run_storage(temp_db, project_id, repo)
    artifacts = AskArtifactStore(tmp_path / "state", project_id, run_id)
    issued_paths: list[str] = []
    probes: list[tuple[list[str], Path, float]] = []
    runtime_workspaces: list[Path] = []

    class Credentials:
        def issue_tool_request(
            self, *, session_id: UUID, requested_project_path: str, expires_at: datetime
        ) -> ManagedToolCredential:
            assert str(session_id) == storage.execution_inputs(run_id)["caller_session_id"]
            issued_paths.append(requested_project_path)
            return ManagedToolCredential(
                ManagedCredential(
                    uuid4(), "test_role", 1, datetime.now(UTC), expires_at, tmp_path / "grant"
                ),
                UUID(project_id),
                requested_project_path,
            )

        def revoke(self, *args: object, **kwargs: object) -> None:
            pass

    def runtime(*, workspace: Path, **kwargs: object) -> CodeIndexPreflightResult:
        runtime_workspaces.append(workspace)
        return CodeIndexPreflightResult(env={}, runtime_home=str(artifacts.run_root / "home"))

    async def run(argv: list[str], *, cwd: Path, timeout: float, **kwargs: object) -> None:
        assert not {"index", "--snapshot-commit", "--snapshot-json"}.intersection(argv)
        assert argv[argv.index("--project") + 1] == str(repo.resolve())
        probes.append((argv, cwd, timeout))

    monkeypatch.setattr(snapshot_module, "_prepare_gcode_runtime", runtime)
    monkeypatch.setattr(snapshot_module, "_run_gcode", run)
    manager = AskSnapshotManager(
        run_storage=storage,
        credential_manager=cast(ManagedCredentialManager, Credentials()),
        snapshot_executable=Path("/usr/bin/true"),
    )
    prepared = await manager.prepare_async(run_id=run_id, repository_root=repo, artifacts=artifacts)
    assert issued_paths == [str(repo.resolve())]
    assert [argv[1] for argv, _, _ in probes] == ["status", "search-content"]
    assert all(cwd == repo.resolve() for _, cwd, _ in probes)
    assert 0 < probes[0][2] <= 5
    assert 0 < probes[1][2] <= 10
    assert runtime_workspaces == [artifacts.run_root / "runtime"]
    assert prepared.runtime.env["GOBBY_PROJECT_ID"] == project_id
    assert (
        prepared.runtime.env["GOBBY_SESSION_ID"]
        == storage.execution_inputs(run_id)["caller_session_id"]
    )


async def test_bind_timeout_never_publishes_generation(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repository(tmp_path)
    project_id = str(sample_project["id"])
    storage, run_id = _run_storage(temp_db, project_id, repo)
    artifacts = AskArtifactStore(tmp_path / "state", project_id, run_id)
    monkeypatch.setattr(snapshot_module, "_CONFIG_PROBE_TIMEOUT", 0.01)
    monkeypatch.setattr(snapshot_module, "_SEARCH_SMOKE_TIMEOUT", 0.01)

    async def stalled(root: Path, deadline: datetime) -> SnapshotIndexRuntime:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    manager = AskSnapshotManager(
        run_storage=storage, index_preparer=stalled, snapshot_executable=Path("/usr/bin/true")
    )
    with pytest.raises(TimeoutError):
        await manager.prepare_async(run_id=run_id, repository_root=repo, artifacts=artifacts)
    record = storage.get(run_id)
    assert record is not None and record.generation is None
    assert not (artifacts.run_root / "source").exists()


def test_commit_ref_is_rejected() -> None:
    with pytest.raises(ValidationError, match="commit_ref"):
        AskRequest.model_validate(
            {"question": "Where?", "project_id": "project", "commit_ref": "HEAD~1"}
        )


async def test_cancel_during_grant_waits_and_revokes_before_returning(
    temp_db: HubDatabase, sample_project: dict[str, object], tmp_path: Path
) -> None:
    repo = _repository(tmp_path)
    project_id = str(sample_project["id"])
    storage, run_id = _run_storage(temp_db, project_id, repo)
    artifacts = AskArtifactStore(tmp_path / "state", project_id, run_id)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    execution_id = uuid4()
    revoked: list[tuple[UUID, int, str]] = []

    class Credentials:
        def issue_tool_request(
            self, *, session_id: UUID, requested_project_path: str, expires_at: datetime
        ) -> ManagedToolCredential:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(10):
                raise TimeoutError("test did not release credential issuance")
            return ManagedToolCredential(
                ManagedCredential(
                    execution_id, "test_role", 7, datetime.now(UTC), expires_at, tmp_path / "grant"
                ),
                UUID(project_id),
                requested_project_path,
            )

        def revoke(self, identifier: UUID, *, generation: int, reason: str) -> None:
            revoked.append((identifier, generation, reason))

    manager = AskSnapshotManager(
        run_storage=storage,
        credential_manager=cast(ManagedCredentialManager, Credentials()),
        snapshot_executable=Path("/usr/bin/true"),
    )
    task = asyncio.create_task(
        manager.prepare_async(run_id=run_id, repository_root=repo, artifacts=artifacts)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        checkpoint = asyncio.Event()
        loop.call_soon(checkpoint.set)
        await checkpoint.wait()
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert revoked == [(execution_id, 7, "ask_binding_cancelled")]
    record = storage.get(run_id)
    assert record is not None
    assert record.generation is None


@pytest.mark.parametrize("recover", [False, True])
async def test_cancel_during_publication_preserves_committed_authority(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recover: bool,
) -> None:
    repo = _repository(tmp_path)
    project_id = str(sample_project["id"])
    storage, run_id = _run_storage(temp_db, project_id, repo)
    artifacts = AskArtifactStore(tmp_path / "state", project_id, run_id)
    runtimes: list[SnapshotIndexRuntime] = []
    released: list[SnapshotIndexRuntime] = []
    revoked: list[tuple[UUID, int, str]] = []

    class Credentials:
        def revoke(self, identifier: UUID, *, generation: int, reason: str) -> None:
            revoked.append((identifier, generation, reason))

    async def probe(root: Path, deadline: datetime) -> SnapshotIndexRuntime:
        runtime = SnapshotIndexRuntime(Path("/usr/bin/true"), {}, str(uuid4()), 1)
        runtimes.append(runtime)
        return runtime

    manager = AskSnapshotManager(
        run_storage=storage,
        index_preparer=probe,
        index_releaser=released.append,
        credential_manager=cast(ManagedCredentialManager, Credentials()),
    )
    if recover:
        await manager.prepare_async(run_id=run_id, repository_root=repo, artifacts=artifacts)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = storage.publish_snapshot_generation

    def publish(identifier: str, **kwargs: Any) -> SnapshotGeneration:
        result = original(identifier, **kwargs)
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(10):
            raise TimeoutError("test did not release generation publication")
        return result

    monkeypatch.setattr(storage, "publish_snapshot_generation", publish)
    task = asyncio.create_task(
        manager.recover_async(run_id=run_id, artifacts=artifacts)
        if recover
        else manager.prepare_async(run_id=run_id, repository_root=repo, artifacts=artifacts)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    record = storage.get(run_id)
    assert record is not None
    current = record.generation
    assert current is not None
    assert current.generation == (2 if recover else 1)
    assert runtimes[-1] not in released
    body = artifacts.read_body(current.lifecycle_artifact)
    assert body["managed_execution_id"] == runtimes[-1].managed_execution_id
    assert revoked == (
        [(UUID(runtimes[0].managed_execution_id), 1, "ask_binding_recovered")] if recover else []
    )
