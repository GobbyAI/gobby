"""Real managed Ask snapshot and native evidence integration."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from gobby.agents.code_index import ensure_isolation_code_index
from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.contracts import AskRequest, ProfileSnapshot
from gobby.ask.evidence import EvidenceAdmission, EvidenceAdmissionError
from gobby.ask.snapshots import AskSnapshotManager, _native_snapshot
from gobby.ask.storage import AskRunStorage
from gobby.runtime_grants.service import DeploymentGrantContext
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.managed_credentials import ManagedCredentialManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.sessions import SessionManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils import machine_id as machine_identity
from tests.ask import native_probe_harness as harness
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.integration


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=10
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Ask Integration",
        "-c",
        "user.email=ask-integration@example.invalid",
        "commit",
        "--no-gpg-sign",
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


def _branch_gcode() -> Path:
    native_bin_dir = os.environ.get("GOBBY_NATIVE_BIN_DIR")
    executable = (
        Path(native_bin_dir) / "gcode"
        if native_bin_dir
        else Path(__file__).parents[2] / "target" / "debug" / "gcode"
    )
    assert executable.is_file(), "build the branch-local gcode binary before integration"
    return executable.resolve(strict=True)


@pytest.mark.parametrize("failure_kind", ["timeout", "nonzero"])
def test_private_parent_index_records_snapshot_failure_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    evidence_path = tmp_path / "parent-index-bootstrap.json"
    gcode_bin = tmp_path / "gcode"
    gcode_bin.write_bytes(b"private gcode fixture")
    gcode_bin.chmod(0o700)
    real_run = subprocess.run

    def fail_snapshot(
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        if command[0] == str(gcode_bin) and "evidence" in command:
            if failure_kind == "timeout":
                raise subprocess.TimeoutExpired(
                    cmd=command,
                    timeout=1.0,
                    output=b"partial stdout",
                    stderr=b"partial stderr",
                )
            return subprocess.CompletedProcess(
                command,
                17,
                stdout=b"partial stdout",
                stderr=b"partial stderr",
            )
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_snapshot)

    expected_error = subprocess.TimeoutExpired if failure_kind == "timeout" else RuntimeError
    with pytest.raises(expected_error):
        harness._provision_private_parent_index(
            project_root=repo,
            manager=object(),
            database=object(),
            session_id=UUID("00000000-0000-0000-0000-000000000001"),
            project_id="00000000-0000-0000-0000-000000000002",
            machine_id="00000000-0000-0000-0000-000000000003",
            runtime_root=tmp_path / "private-parent-index",
            gcode_bin=gcode_bin,
            source_commit="0" * 40,
            deadline_monotonic=time.monotonic() + 10,
            database_scope="gobby_test_timeout_receipt",
            evidence_path=evidence_path,
        )

    evidence = json.loads(evidence_path.read_bytes())
    assert evidence["status"] == "failed"
    assert evidence["gcode"] == {
        "path": str(gcode_bin),
        "sha256": hashlib.sha256(b"private gcode fixture").hexdigest(),
    }
    receipt = next(
        command for command in evidence["commands"] if command["name"] == "snapshot_materialize"
    )
    assert receipt["argv"][0] == str(gcode_bin)
    assert receipt["returncode"] == (None if failure_kind == "timeout" else 17)
    assert receipt["stdout_sha256"] == hashlib.sha256(b"partial stdout").hexdigest()
    assert receipt["stderr_sha256"] == hashlib.sha256(b"partial stderr").hexdigest()
    assert receipt["error"]["error_type"] == (
        "TimeoutExpired" if failure_kind == "timeout" else "SubprocessExitError"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_parent_index", [True, False])
async def test_real_managed_snapshot_queries_branch_native_gcode(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_parent_index: bool,
) -> None:
    host_machine_id = machine_identity.require_machine_id()
    private_machine_id = str(uuid4())
    assert private_machine_id != host_machine_id
    private_home = tmp_path / "private-gobby-home"
    private_home.mkdir()
    harness._seed_contained_machine_identity(private_home, private_machine_id)
    monkeypatch.setenv("GOBBY_HOME", str(private_home))
    monkeypatch.setattr(machine_identity, "_cached_machine_id", None)
    assert machine_identity.require_machine_id() == private_machine_id
    isolated = isolated_checkout_factory(
        temp_db,
        "ask-native-integration",
        root=tmp_path / "repository",
    )
    assert isolated.machine_id == private_machine_id
    repo = Path(isolated.root_path)
    _git(repo, "init", "--quiet", "-b", "main")
    (repo / "src").mkdir()
    (repo / "include").mkdir()
    (repo / "assets").mkdir()
    (repo / ".metadata").mkdir()
    (repo / ".metadata" / "project.json").write_text(
        '{"project": "native evidence fixture"}\n', encoding="utf-8"
    )
    (repo / "target").mkdir()
    (repo / "target" / "committed.txt").write_text("committed build-directory text\n")
    (repo / "src" / "empty.py").touch()
    (repo / ".gitignore").write_text("tracked_ignored.py\n")
    (repo / "tracked_ignored.py").write_text("def tracked_ignored_symbol(): pass\n")
    _git(repo, "add", "--force", "tracked_ignored.py")
    (repo / "src" / "lib.rs").write_text(
        "pub fn pinned_symbol() -> usize { 42 }\npub fn second_symbol() -> usize { 7 }\n",
        encoding="utf-8",
    )
    (repo / "include" / "public.h").write_text("#define GOBBY_PUBLIC_HEADER 42\n", encoding="utf-8")
    (repo / "assets" / "raw.txt").write_text("raw identity bytes\n", encoding="utf-8")
    (repo / "src" / "public_config.rs").write_text(
        'const DATABASE: &str = "postgresql://worker:successful-secret@127.0.0.1/db";\n',
        encoding="utf-8",
    )
    root_commit = _commit(repo, "root")
    (repo / "src" / "main.rs").write_text("pub fn main_parent() {}\n", encoding="utf-8")
    _commit(repo, "main parent")
    _git(repo, "checkout", "--quiet", "-b", "feature", root_commit)
    (repo / "src" / "feature.rs").write_text("pub fn feature_parent() {}\n", encoding="utf-8")
    _commit(repo, "feature parent")
    _git(repo, "checkout", "--quiet", "main")
    _git(
        repo,
        "-c",
        "user.name=Ask Integration",
        "-c",
        "user.email=ask-integration@example.invalid",
        "merge",
        "--no-ff",
        "--no-gpg-sign",
        "feature",
        "-m",
        "historical merge",
    )
    historical_merge = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "tip.rs").write_text("pub fn later_tip() {}\n", encoding="utf-8")
    _commit(repo, "later tip")
    (repo / "src" / "dirty.rs").write_text(
        "pub fn dirty_parent_only() {}\n",
        encoding="utf-8",
    )

    project_id = isolated.project.id
    session = SessionManager(temp_db).register(
        external_id="ask-native-integration",
        machine_id=isolated.machine_id,
        source="codex",
        project_id=project_id,
        workspace_path=str(repo),
    )
    storage = AskRunStorage(
        LocalPipelineExecutionManager(temp_db, project_id=project_id),
        profile_resolver=_profile,
    )
    record = storage.start(
        AskRequest(
            question="Where is pinned_symbol?",
            project_id=project_id,
            commit_ref=historical_merge,
            investigator_profile="investigator",
            reviewer_profile="reviewer",
        ),
        repo,
    )
    storage.bind_execution_context(
        record.run_id,
        project_root=repo,
        caller_session_id=session.id,
    )
    assert record.request.timeout_seconds == 600
    artifacts = AskArtifactStore(tmp_path / "state", project_id, record.run_id)
    runtime_root = tmp_path / "managed-runtimes"
    credential_manager = ManagedCredentialManager(
        database=temp_db,
        machine_id=UUID(isolated.machine_id),
        runtime_root=runtime_root,
    )
    source_home = Path(os.environ["GOBBY_HOME"])
    source_home.mkdir(parents=True, exist_ok=True)
    (source_home / "local_cli_token").write_text("isolated-operator-token\n", encoding="utf-8")
    (source_home / "local_cli_token").chmod(0o600)
    monkeypatch.setattr(
        "gobby.agents.code_index._active_deployment_grant_context",
        lambda: DeploymentGrantContext(
            token="a" * 16,
            fencing_epoch=1,
            signing_secret="ask-native-integration-signing-secret",
        ),
    )
    monkeypatch.setattr(
        "gobby.agents.code_index.resolve_native_bin",
        lambda _name: (_ for _ in ()).throw(AssertionError("global gcode lookup")),
    )
    session_id = UUID(session.id)
    gcode_bin = _branch_gcode()
    schema_row = temp_db.fetchone("SELECT current_schema() AS schema")
    assert schema_row is not None
    database_scope = str(schema_row["schema"])
    if initial_parent_index:
        bootstrap = harness._provision_private_parent_index(
            project_root=repo,
            manager=credential_manager,
            database=temp_db,
            session_id=session_id,
            project_id=project_id,
            machine_id=isolated.machine_id,
            runtime_root=runtime_root / "parent",
            gcode_bin=gcode_bin,
            source_commit=historical_merge,
            deadline_monotonic=time.monotonic() + 600,
            database_scope=database_scope,
            evidence_path=tmp_path / "parent-index-bootstrap.json",
        )
        assert bootstrap["status"] == "completed"
        assert bootstrap["source_commit"] == historical_merge
        assert bootstrap["database_scope"] == database_scope
        gcode_identity = cast(dict[str, object], bootstrap["gcode"])
        assert gcode_identity["path"] == str(gcode_bin)
        with gcode_bin.open("rb") as stream:
            assert gcode_identity["sha256"] == hashlib.file_digest(stream, "sha256").hexdigest()
        assert bootstrap["credential_project_id"] == project_id
        assert bootstrap["credential_project_path"] == str(repo.resolve())
        assert bootstrap["checkout_root_before"] == str(repo.resolve())
        assert bootstrap["checkout_root_during_index"] == bootstrap["seed_root"]
        assert bootstrap["checkout_root_after"] == str(repo.resolve())
        assert bootstrap["indexed_root"] == bootstrap["seed_root"]
        indexed_file_count = bootstrap["indexed_file_count"]
        assert isinstance(indexed_file_count, int)
        assert indexed_file_count > 0
        assert bootstrap["credential_revoked"] is True
        commands = cast(list[dict[str, object]], bootstrap["commands"])
        assert [command["name"] for command in commands] == [
            "source_status_before",
            "snapshot_materialize",
            "status_before",
            "index",
            "status_after",
            "source_status_after",
        ]
        assert all(command["returncode"] == 0 for command in commands)
        assert bootstrap["source_status_before"] == bootstrap["source_status_after"]
        restored_checkout = LocalProjectCheckoutManager(temp_db).get(
            isolated.machine_id,
            project_id,
        )
        assert restored_checkout is not None
        assert Path(restored_checkout.root_path).resolve() == repo.resolve()
        indexed_parent = temp_db.fetchone(
            """SELECT root_path FROM code_indexed_project_states
            WHERE machine_id = %s AND project_id = %s""",
            (isolated.machine_id, project_id),
        )
        assert indexed_parent is not None
        seed_root = bootstrap["seed_root"]
        assert isinstance(seed_root, str)
        assert Path(indexed_parent["root_path"]).resolve() == Path(seed_root)
    manager = AskSnapshotManager(
        worktree_storage=LocalWorktreeManager(temp_db),
        run_storage=storage,
        snapshot_executable=gcode_bin,
        credential_manager=credential_manager,
    )

    snapshot = await manager.prepare_async(
        run_id=record.run_id,
        repository_root=repo,
        artifacts=artifacts,
    )
    materialized_binding, _inventory = _native_snapshot(
        gcode_bin,
        snapshot.source_root,
        project_id=str(snapshot.binding["project_id"]),
        commit_oid=historical_merge,
        action="inspect",
        timeout=10,
    )
    assert materialized_binding == snapshot.binding
    admission = EvidenceAdmission(
        run_id=record.run_id,
        runtime=snapshot.runtime,
        permitted_operations={"search", "read"},
        page_size=128 * 1024,
        artifacts=artifacts,
        storage=storage,
    )
    # A parent file first indexed after capture must not enter snapshot reads.
    (repo / "src" / "future.rs").write_text("pub fn future_parent_only() {}\n")
    _commit(repo, "parent changed after snapshot capture")
    parent_session = SessionManager(temp_db).register(
        external_id="ask-native-parent-refresh",
        machine_id=isolated.machine_id,
        source="codex",
        project_id=project_id,
        workspace_path=str(repo),
    )
    parent_credential = credential_manager.issue_tool_request(
        session_id=UUID(parent_session.id),
        requested_project_path=str(repo),
        expires_at=record.binding.deadline_at,
    )
    try:
        await ensure_isolation_code_index(
            str(repo),
            gcode_bin=gcode_bin,
            credential=parent_credential.credential,
            principal_kind="tool_chat",
            runtime_root=runtime_root / "parent-after-capture",
            identity_env={
                "GOBBY_AGENT_RUN_ID": str(parent_credential.credential.managed_execution_id),
                "GOBBY_MACHINE_ID": isolated.machine_id,
                "GOBBY_PROJECT_ID": project_id,
                "GOBBY_SESSION_ID": parent_session.id,
            },
        )
    finally:
        credential_manager.revoke(
            parent_credential.credential.managed_execution_id,
            generation=parent_credential.credential.credential_generation,
            reason="test_parent_refresh_complete",
        )
    assert (
        temp_db.fetchone(
            """SELECT file_path FROM code_indexed_file_states
        WHERE machine_id = %s AND project_id = %s AND file_path = %s""",
            (isolated.machine_id, project_id, "src/future.rs"),
        )
        is not None
    )
    future_parent = await admission.query(
        "search",
        {"lane": "literal", "query": "future_parent_only", "paths": [], "limit": 100},
    )
    assert future_parent["items"] == []
    response = await admission.query(
        "search",
        {"lane": "literal", "query": "pinned_symbol", "paths": [], "limit": 100},
    )
    header = await admission.query(
        "read",
        {"kind": "range", "path": "include/public.h", "start_line": 1, "end_line": 1},
    )
    hidden_metadata = await admission.query(
        "read",
        {"kind": "range", "path": ".metadata/project.json", "start_line": 1, "end_line": 1},
    )
    ignored_source = await admission.query(
        "search",
        {"lane": "literal", "query": "tracked_ignored_symbol", "paths": [], "limit": 100},
    )
    build_directory_text = await admission.query(
        "read",
        {"kind": "range", "path": "target/committed.txt", "start_line": 1, "end_line": 1},
    )
    merge_metadata = await admission.query("read", {"kind": "commit_metadata"})
    partial = await admission.query(
        "search",
        {"lane": "literal", "query": "pub fn", "paths": ["src"], "limit": 1},
    )
    newer_parent = await admission.query(
        "search",
        {"lane": "literal", "query": "later_tip", "paths": [], "limit": 100},
    )
    dirty_parent = await admission.query(
        "search",
        {"lane": "literal", "query": "dirty_parent_only", "paths": [], "limit": 100},
    )

    assert snapshot.commit_oid == historical_merge
    assert len(snapshot.binding["commit"]["parent_oids"]) == 2
    assert snapshot.runtime.executable == gcode_bin
    assert any(item.get("path") == "src/lib.rs" for item in response["items"])
    assert header["items"][0]["excerpt"] == "#define GOBBY_PUBLIC_HEADER 42\n"
    assert hidden_metadata["items"][0]["excerpt"] == '{"project": "native evidence fixture"}\n'
    assert any(item.get("path") == "tracked_ignored.py" for item in ignored_source["items"])
    assert build_directory_text["items"][0]["excerpt"] == "committed build-directory text\n"
    empty_entry = next(
        item for item in snapshot.inventory["entries"] if item["path"] == "src/empty.py"
    )
    assert empty_entry["exclusion"] is None
    assert empty_entry["size_bytes"] == 0
    assert merge_metadata["items"][0]["comparison_kind"] == "first_parent"
    assert len(merge_metadata["items"][0]["parent_oids"]) == 2
    assert partial["complete"] is False
    assert partial["completeness"] == "truncated_index"
    assert partial.get("continuation") is None
    assert newer_parent["items"] == []
    assert dirty_parent["items"] == []
    secret_entry = next(
        item for item in snapshot.inventory["entries"] if item["path"] == "src/public_config.rs"
    )
    assert secret_entry["exclusion"] == "sensitive_content"
    assert not (snapshot.source_root / "src" / "public_config.rs").exists()
    assert "successful-secret" not in str(response)

    # The home binding must preserve gcore's independent on-disk identity check.
    harness._seed_contained_machine_identity(private_home, str(uuid4()))
    try:
        with pytest.raises(
            EvidenceAdmissionError, match="grant machine does not match local machine"
        ):
            await admission.query(
                "search",
                {"lane": "literal", "query": "pinned_symbol", "paths": [], "limit": 100},
            )
    finally:
        harness._seed_contained_machine_identity(private_home, private_machine_id)

    previous_execution = UUID(snapshot.runtime.managed_execution_id)
    recovered = await manager.recover_async(run_id=record.run_id, artifacts=artifacts)
    assert recovered.generation == 2
    assert credential_manager.get_live_binding_generation(previous_execution) is None
    with pytest.raises(EvidenceAdmissionError, match="runtime identity"):
        await admission.query(
            "search",
            {"lane": "literal", "query": "pinned_symbol", "paths": [], "limit": 100},
        )
    recovered_admission = EvidenceAdmission(
        run_id=record.run_id,
        runtime=recovered.runtime,
        permitted_operations={"search"},
        page_size=128 * 1024,
        artifacts=artifacts,
        storage=storage,
    )
    recovered_response = await recovered_admission.query(
        "search",
        {"lane": "literal", "query": "pinned_symbol", "paths": [], "limit": 100},
    )
    assert any(item.get("path") == "src/lib.rs" for item in recovered_response["items"])
    manager.release(recovered, artifacts=artifacts)

    root_record = storage.start(
        AskRequest(
            question="What is the root identity?",
            project_id=project_id,
            commit_ref=root_commit,
            investigator_profile="investigator",
            reviewer_profile="reviewer",
            timeout_seconds=120,
        ),
        repo,
    )
    storage.bind_execution_context(
        root_record.run_id,
        project_root=repo,
        caller_session_id=session.id,
    )
    root_artifacts = AskArtifactStore(tmp_path / "state", project_id, root_record.run_id)
    root_snapshot = await manager.prepare_async(
        run_id=root_record.run_id,
        repository_root=repo,
        artifacts=root_artifacts,
    )
    root_admission = EvidenceAdmission(
        run_id=root_record.run_id,
        runtime=root_snapshot.runtime,
        permitted_operations={"read"},
        page_size=128 * 1024,
        artifacts=root_artifacts,
        storage=storage,
    )
    root_metadata = await root_admission.query("read", {"kind": "commit_metadata"})
    raw = await root_admission.query(
        "read",
        {"kind": "range", "path": "assets/raw.txt", "start_line": 1, "end_line": 1},
    )
    assert root_metadata["items"][0]["parent_oids"] == []
    assert root_metadata["items"][0]["comparison_kind"] == "empty_tree"
    assert raw["items"][0]["excerpt"] == "raw identity bytes\n"
    assert raw["items"][0]["blob_oid"]
    assert raw["items"][0]["content_hash"]
    manager.release(root_snapshot, artifacts=root_artifacts)
