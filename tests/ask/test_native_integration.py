"""Real managed Ask snapshot and native evidence integration."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from gobby.agents.code_index import ensure_isolation_code_index
from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.contracts import AskRequest, ProfileSnapshot
from gobby.ask.evidence import EvidenceAdmission, EvidenceAdmissionError
from gobby.ask.snapshots import AskSnapshotManager
from gobby.ask.storage import AskRunStorage
from gobby.runtime_grants.service import DeploymentGrantContext
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.managed_credentials import ManagedCredentialManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
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


@pytest.mark.asyncio
async def test_real_managed_live_index_queries_branch_native_gcode(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    initial_credential = credential_manager.issue_tool_request(
        session_id=session_id,
        requested_project_path=str(repo),
        expires_at=record.binding.deadline_at,
    )
    try:
        await ensure_isolation_code_index(
            str(repo),
            gcode_bin=gcode_bin,
            credential=initial_credential.credential,
            principal_kind="tool_chat",
            runtime_root=runtime_root / "initial-index",
            identity_env={
                "GOBBY_AGENT_RUN_ID": str(initial_credential.credential.managed_execution_id),
                "GOBBY_MACHINE_ID": isolated.machine_id,
                "GOBBY_PROJECT_ID": project_id,
                "GOBBY_SESSION_ID": session.id,
            },
        )
    finally:
        credential_manager.revoke(
            initial_credential.credential.managed_execution_id,
            generation=initial_credential.credential.credential_generation,
            reason="fixture_indexed",
        )
    manager = AskSnapshotManager(
        run_storage=storage,
        snapshot_executable=gcode_bin,
        credential_manager=credential_manager,
    )

    snapshot = await manager.prepare_async(
        run_id=record.run_id,
        repository_root=repo,
        artifacts=artifacts,
    )
    assert snapshot.binding["project_id"] == project_id
    assert snapshot.source_root == repo.resolve()
    assert not (artifacts.run_root / "source").exists()
    admission = EvidenceAdmission(
        run_id=record.run_id,
        runtime=snapshot.runtime,
        permitted_operations={"search", "read"},
        page_size=128 * 1024,
        artifacts=artifacts,
        storage=storage,
    )
    # The caller index remains live across ordinary incremental refreshes.
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
    assert any(item.get("path") == "src/future.rs" for item in future_parent["items"])
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
    dirty_parent = await admission.query(
        "search",
        {"lane": "literal", "query": "dirty_parent_only", "paths": [], "limit": 100},
    )

    assert snapshot.commit_oid == historical_merge
    assert snapshot.runtime.executable == gcode_bin
    assert any(item.get("path") == "src/lib.rs" for item in response["items"])
    assert header["items"][0]["excerpt"] == "#define GOBBY_PUBLIC_HEADER 42\n"
    assert merge_metadata["items"][0]["comparison_kind"] == "first_parent"
    assert len(merge_metadata["items"][0]["parent_oids"]) == 2
    assert partial["complete"] is False
    assert partial["completeness"] == "truncated_index"
    assert partial.get("continuation") is None
    assert any(item.get("path") == "src/dirty.rs" for item in dirty_parent["items"])
    configuration = await admission.query(
        "read",
        {
            "kind": "range",
            "path": "src/public_config.rs",
            "start_line": 1,
            "end_line": 1,
        },
    )
    assert "successful-secret" in configuration["items"][0]["excerpt"]

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
    assert (
        temp_db.fetchone(
            """SELECT file_path FROM code_indexed_file_states
        WHERE machine_id = %s AND project_id = %s AND file_path = %s""",
            (isolated.machine_id, project_id, "src/future.rs"),
        )
        is not None
    )
    manager.release(recovered, artifacts=artifacts)
