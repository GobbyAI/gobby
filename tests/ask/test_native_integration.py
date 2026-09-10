"""Real managed Ask snapshot and native evidence integration."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from uuid import UUID

import pytest

from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.contracts import AskRequest, ProfileSnapshot
from gobby.ask.evidence import EvidenceAdmission, EvidenceAdmissionError
from gobby.ask.snapshots import AskSnapshotManager, _native_snapshot
from gobby.ask.storage import AskRunStorage
from gobby.runtime_grants.service import DeploymentGrantContext
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.managed_credentials import ManagedCredentialManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.worktrees import LocalWorktreeManager
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
    executable = Path(__file__).parents[2] / "target" / "debug" / "gcode"
    assert executable.is_file(), "build the branch-local gcode binary before integration"
    return executable


@pytest.mark.asyncio
async def test_real_managed_snapshot_queries_branch_native_gcode(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated = isolated_checkout_factory(
        temp_db,
        "ask-native-integration",
        root=tmp_path / "repository",
    )
    repo = Path(isolated.root_path)
    _git(repo, "init", "--quiet", "-b", "main")
    (repo / "src").mkdir()
    (repo / "include").mkdir()
    (repo / "assets").mkdir()
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
    assert all(command["returncode"] == 0 for command in bootstrap["commands"])
    assert bootstrap["source_status_before"] == bootstrap["source_status_after"]
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
    response = await admission.query(
        "search",
        {"lane": "literal", "query": "pinned_symbol", "paths": [], "limit": 100},
    )
    header = await admission.query(
        "read",
        {"kind": "range", "path": "include/public.h", "start_line": 1, "end_line": 1},
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
