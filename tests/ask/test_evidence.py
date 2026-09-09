from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.contracts import AskRequest, AskRunRecord, ProfileSnapshot, RetrievalMode
from gobby.ask.evidence import EvidenceAdmission, EvidenceAdmissionError
from gobby.ask.snapshots import SnapshotIndexRuntime
from gobby.ask.storage import AskRunStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager

pytestmark = pytest.mark.unit


def _write_gcode_fixture(path: Path) -> None:
    path.write_text(
        """
import hashlib
import json
import sys
import time

request = json.loads(sys.argv[sys.argv.index('--request-json') + 1])
query = request.get('search', {}).get('query')
if query == 'failure':
    print(json.dumps({
        'error': 'index_unavailable',
        'message': 'postgresql://worker:super-secret@127.0.0.1/db unavailable',
        'recovery': 'retry later',
    }), file=sys.stderr)
    raise SystemExit(2)
if query == 'timeout':
    time.sleep(5)
response = {
    'request': request,
    'request_fingerprint': hashlib.sha256(
        json.dumps(request, separators=(',', ':')).encode()
    ).hexdigest(),
    'binding': request['binding'],
    'contract': {
        'name': 'gcode-evidence',
        'schema_version': 1,
        'tool': 'gobby-code',
        'tool_version': '0.5.0',
        'lane': 'literal_search',
    },
    'items': [{'kind': 'source', 'evidence_id': 'src:' + (query or 'read')}],
    'complete': True,
    'completeness': 'complete',
    'bounds': {
        'max_bytes': request['max_bytes'],
        'serialized_item_bytes': 10,
        'returned_items': 1,
        'total_items': 1,
        'result_limit': request.get('search', {}).get('limit', 1000),
    },
    'exclusions': [],
    'warnings': [],
    'continuation': None,
}
if query == 'identity-mismatch':
    response['binding'] = {**request['binding'], 'commit_oid': '0' * 40}
if query == 'terminal-partial':
    response['complete'] = False
    response['completeness'] = 'truncated_index'
print(json.dumps(response, separators=(',', ':')))
""",
        encoding="utf-8",
    )


def _profile(identifier: str, _timeout: float) -> ProfileSnapshot:
    return ProfileSnapshot(
        identifier=identifier,
        definition_id=f"definition-{identifier}",
        definition_updated_at="2026-09-08T12:00:00+00:00",
        effective={"name": identifier, "provider": "codex", "model": "gpt-test"},
    )


def _persist_authority(
    manager: LocalPipelineExecutionManager,
    *,
    project_id: str,
    state_root: Path,
    executable: Path,
    argv_prefix: tuple[str, ...],
    timeout_seconds: float,
    suffix: str,
) -> tuple[
    AskRunStorage,
    AskArtifactStore,
    AskRunRecord,
    dict[str, Any],
    Path,
    SnapshotIndexRuntime,
    dict[str, Any],
]:
    def resolve_commit(_root: Path, _ref: str, _timeout: float) -> tuple[str, str]:
        return "a" * 40, "b" * 40

    storage = AskRunStorage(
        manager,
        commit_resolver=resolve_commit,
        profile_resolver=_profile,
    )
    record = storage.start(
        AskRequest(
            question="Where is the source of truth?",
            project_id=project_id,
            timeout_seconds=timeout_seconds,
            investigator_profile="investigator",
            reviewer_profile="reviewer",
            idempotency_key=f"evidence-{suffix}",
        ),
        state_root,
    )
    artifacts = AskArtifactStore(state_root, project_id, record.run_id)
    source_root = artifacts.run_root / "source"
    source_root.mkdir()
    binding = {
        "project_id": project_id,
        "commit_oid": record.binding.commit_oid,
        "tree_oid": record.binding.tree_oid,
        "inventory_digest": "c" * 64,
        "commit": {
            "parent_oids": [],
            "comparison_parent_oid": "d" * 40,
            "comparison_kind": "empty_tree",
            "changed_paths_digest": "e" * 64,
            "changed_paths": [],
        },
    }
    deadline_at = record.binding.deadline_at.isoformat().replace("+00:00", "Z")
    identity = {
        "schema_version": 1,
        "run_id": record.run_id,
        "project_id": project_id,
        "commit_oid": record.binding.commit_oid,
        "deadline_at": deadline_at,
        "retrieval_mode": record.binding.retrieval_mode.value,
        "binding": binding,
        "inventory": {"digest": binding["inventory_digest"], "entries": []},
    }
    identity_pointer = artifacts.write_body("snapshot-identity", identity)
    record = storage.attach_snapshot(
        record.run_id,
        inventory_digest=str(binding["inventory_digest"]),
        snapshot_artifact=identity_pointer,
        deadline_at=record.binding.deadline_at,
    )
    runtime = SnapshotIndexRuntime(
        executable=executable,
        env={"DATABASE_URL": "postgresql://worker:super-secret@127.0.0.1/db"},
        managed_execution_id=f"managed-{suffix}",
        credential_generation=1,
        argv_prefix=argv_prefix,
    )
    lifecycle = {
        "schema_version": 1,
        "generation": 1,
        "run_id": record.run_id,
        "project_id": project_id,
        "commit_oid": record.binding.commit_oid,
        "deadline_at": deadline_at,
        "retrieval_mode": record.binding.retrieval_mode.value,
        "inventory_digest": binding["inventory_digest"],
        "snapshot_artifact": identity_pointer,
        "repository_root": str(state_root),
        "source_root": str(source_root),
        "worktree_id": f"worktree-{suffix}",
        "index_runtime": {
            "executable": str(executable),
            "argv_prefix": list(argv_prefix),
            "managed_execution_id": runtime.managed_execution_id,
            "credential_generation": runtime.credential_generation,
        },
    }
    lifecycle_pointer = artifacts.write_body("snapshot-lifecycle", lifecycle)
    storage.publish_snapshot_generation(
        record.run_id,
        generation=1,
        lifecycle_artifact=lifecycle_pointer,
        expected_previous_generation=None,
        deadline_at=record.binding.deadline_at,
    )
    return storage, artifacts, record, binding, source_root, runtime, lifecycle


@pytest.mark.asyncio
async def test_durable_scoped_evidence_admission(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    project_id = str(sample_project["id"])
    execution_manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    script = tmp_path / "gcode_fixture.py"
    _write_gcode_fixture(script)
    executable = Path(sys.executable)
    argv_prefix = ("-S", str(script))
    storage, artifacts, record, binding, source_root, runtime, lifecycle = _persist_authority(
        execution_manager,
        project_id=project_id,
        state_root=tmp_path / "state",
        executable=executable,
        argv_prefix=argv_prefix,
        timeout_seconds=600,
        suffix="main",
    )
    mismatched_runtime = SnapshotIndexRuntime(
        executable=runtime.executable,
        env=runtime.env,
        managed_execution_id="unpersisted-runtime",
        credential_generation=runtime.credential_generation,
        argv_prefix=runtime.argv_prefix,
    )
    with pytest.raises(EvidenceAdmissionError, match="runtime identity"):
        EvidenceAdmission(
            run_id=record.run_id,
            runtime=mismatched_runtime,
            permitted_operations={"search"},
            page_size=4096,
            artifacts=artifacts,
            storage=storage,
        )
    admission = EvidenceAdmission(
        run_id=record.run_id,
        runtime=runtime,
        permitted_operations={"search", "read", "graph"},
        page_size=4096,
        artifacts=artifacts,
        storage=storage,
    )
    assert admission.source_root == source_root
    assert admission.snapshot_binding == binding
    assert admission.retrieval_mode is RetrievalMode.DETERMINISTIC
    assert admission.deadline_at == record.binding.deadline_at

    requests = [
        {
            "search": {
                "lane": "literal",
                "query": f"query-{index}",
                "paths": ["src"],
                "language": None,
                "kind": None,
                "limit": 1000,
                "hybrid_identity": None,
            }
        }
        for index in range(35)
    ]
    responses = await asyncio.gather(*(admission.query("search", request) for request in requests))
    assert len(responses) == 35
    assert all(response["binding"] == binding for response in responses)

    terminal_partial = await admission.query(
        "search",
        {
            "search": {
                "lane": "literal",
                "query": "terminal-partial",
                "paths": [],
                "limit": 1000,
            }
        },
    )
    assert terminal_partial["complete"] is False
    assert terminal_partial["completeness"] == "truncated_index"
    assert terminal_partial["continuation"] is None

    with pytest.raises(EvidenceAdmissionError, match="index_unavailable"):
        await admission.query(
            "search",
            {
                "search": {
                    "lane": "literal",
                    "query": "failure",
                    "paths": [],
                    "language": None,
                    "kind": None,
                    "limit": 1000,
                    "hybrid_identity": None,
                }
            },
        )
    sensitive_operations: list[tuple[str, dict[str, Any]]] = [
        ("search", {"search": {"lane": "literal", "query": "secret", "paths": [".env"]}}),
        ("read", {"read": {"range": {"path": ".env", "start_line": 1, "end_line": 1}}}),
        ("graph", {"graph": {"query": "imports", "entity": {"path": ".env"}}}),
    ]
    for operation, selector in sensitive_operations:
        with pytest.raises(EvidenceAdmissionError, match="sensitive path"):
            await admission.query(operation, selector)
    with pytest.raises(EvidenceAdmissionError, match="escapes the snapshot root"):
        await admission.query(
            "read",
            {"read": {"range": {"path": "../outside", "start_line": 1, "end_line": 1}}},
        )
    with pytest.raises(EvidenceAdmissionError, match="snapshot binding"):
        await admission.query(
            "search",
            {
                "search": {
                    "lane": "literal",
                    "query": "identity-mismatch",
                    "paths": [],
                    "limit": 1000,
                }
            },
        )

    (
        timed_storage,
        timed_artifacts,
        timed_record,
        _,
        _,
        timed_runtime,
        _,
    ) = _persist_authority(
        execution_manager,
        project_id=project_id,
        state_root=tmp_path / "timed-state",
        executable=executable,
        argv_prefix=argv_prefix,
        timeout_seconds=2,
        suffix="timed",
    )
    timed = EvidenceAdmission(
        run_id=timed_record.run_id,
        runtime=timed_runtime,
        permitted_operations={"search"},
        page_size=4096,
        artifacts=timed_artifacts,
        storage=timed_storage,
    )
    with pytest.raises(EvidenceAdmissionError, match="deadline exceeded"):
        await timed.query(
            "search",
            {
                "search": {
                    "lane": "literal",
                    "query": "timeout",
                    "paths": [],
                    "language": None,
                    "kind": None,
                    "limit": 1000,
                    "hybrid_identity": None,
                }
            },
        )

    persisted = execution_manager.get_execution(record.run_id)
    assert persisted is not None
    checkpoint = json.loads(persisted.outputs_json or "{}")["ask"]["evidence"]
    assert len(checkpoint) == 42
    assert len({item["invocation_id"] for item in checkpoint}) == 42
    assert any(item["status"] == "failed" for item in checkpoint)
    assert any(item["status"] == "denied" for item in checkpoint)
    assert any(item["status"] == "invalid_response" for item in checkpoint)
    assert all(item["usage"] is None for item in checkpoint)
    assert all(
        item["snapshot_inventory_digest"] == binding["inventory_digest"] for item in checkpoint
    )
    assert all(len(item["request_hash"]) == 64 for item in checkpoint)

    manifest = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["artifacts"]) == 86
    assert oct(artifacts.run_root.stat().st_mode & 0o777) == "0o700"
    assert all(
        oct((artifacts.run_root / item["relative_path"]).stat().st_mode & 0o777) == "0o600"
        for item in manifest["artifacts"]
    )
    bodies = [artifacts.read_body(item["result_artifact"]) for item in checkpoint]
    positions = {item["relative_path"]: index for index, item in enumerate(manifest["artifacts"])}
    assert all(
        positions[item["invocation_artifact"]["relative_path"]]
        < positions[item["result_artifact"]["relative_path"]]
        for item in checkpoint
    )
    assert "super-secret" not in json.dumps(bodies)
    assert not any("shell" in json.dumps(body["argv"]) for body in bodies if "argv" in body)
    assert os.environ.get("DATABASE_URL") != "postgresql://worker:super-secret@127.0.0.1/db"

    timed_persisted = execution_manager.get_execution(timed_record.run_id)
    assert timed_persisted is not None
    timed_checkpoint = json.loads(timed_persisted.outputs_json or "{}")["ask"]["evidence"]
    assert [item["status"] for item in timed_checkpoint] == ["timeout"]

    tampered_lifecycle = {**lifecycle, "generation": 2, "retrieval_mode": "audited_hybrid"}
    tampered_pointer = artifacts.write_body("snapshot-lifecycle", tampered_lifecycle)
    storage.publish_snapshot_generation(
        record.run_id,
        generation=2,
        lifecycle_artifact=tampered_pointer,
        expected_previous_generation=1,
        deadline_at=record.binding.deadline_at,
    )
    with pytest.raises(EvidenceAdmissionError, match="retrieval_mode"):
        EvidenceAdmission(
            run_id=record.run_id,
            runtime=runtime,
            permitted_operations={"search"},
            page_size=4096,
            artifacts=artifacts,
            storage=storage,
        )
