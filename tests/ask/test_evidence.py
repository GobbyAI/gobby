from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.contracts import (
    AskRequest,
    AskRunRecord,
    EvidenceReference,
    ProfileSnapshot,
    RetrievalMode,
)
from gobby.ask.evidence import EvidenceAdmission, EvidenceAdmissionError, _contains_credential
from gobby.ask.snapshots import SnapshotIndexRuntime
from gobby.ask.storage import AskRunStorage
from gobby.code_index.eligibility import code_index_id_for_root
from gobby.runtime_grants.schema import GrantBundle
from gobby.runtime_grants.signing import sign_grant
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager

pytestmark = pytest.mark.unit
_TEST_LITERAL_VALUE = "01234567" + "89abcdef"


@pytest.mark.parametrize(
    ("value", "source_languages", "expected"),
    [
        ({"excerpt": str(Path.home()) + "/example.py"}, None, False),
        (
            {"path": "settings.yaml", "excerpt": f"token = {Path.home()}/x"},
            {"settings.yaml": "yaml"},
            True,
        ),
        ({"excerpt": "token = runtime_token_reference"}, None, False),
        (
            {"path": "src/runtime.py", "excerpt": "token = runtime_token_reference"},
            {"src/runtime.py": "python"},
            False,
        ),
        (
            {"path": "src/runtime.py", "excerpt": "token = " + repr(_TEST_LITERAL_VALUE)},
            {"src/runtime.py": "python"},
            True,
        ),
        (
            {"path": "settings.yaml", "excerpt": "token = runtime_token_reference"},
            {"settings.yaml": "yaml"},
            True,
        ),
        (
            {"path": "settings.json", "excerpt": json.dumps({"token": _TEST_LITERAL_VALUE})},
            {"settings.json": "json"},
            True,
        ),
        (
            {"path": "docs/task-native-source-reference.md", "excerpt": "safe evidence"},
            {"docs/task-native-source-reference.md": None},
            False,
        ),
        ({"excerpt": "postgresql://worker:real-secret@127.0.0.1/db"}, None, True),
    ],
)
def test_credential_classifier_matches_snapshot_source_semantics(
    value: dict[str, str],
    source_languages: dict[str, str | None] | None,
    expected: bool,
) -> None:
    assert _contains_credential(value, source_languages=source_languages) is expected


def _write_gcode_fixture(path: Path) -> None:
    path.write_text(
        """
import hashlib
import json
import sys
import time
from pathlib import Path

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
if query == 'credential-response':
    response['items'][0].update({
        'path': 'src/public.py',
        'excerpt': 'postgresql://worker:successful-secret@127.0.0.1/db',
    })
if query == 'token = runtime_token_reference':
    excerpt = f'source = {Path.home()}/example.py\\ntoken = runtime_token_reference'
    excerpt_hash = hashlib.sha256(excerpt.encode()).hexdigest()
    response['items'][0] = {
        'item_type': 'source',
        'evidence_id': 'src:source-reference-response',
        'path': 'src/runtime.py',
        'blob_oid': 'f' * 40,
        'content_hash': excerpt_hash,
        'excerpt_hash': excerpt_hash,
        'line_start': 1,
        'line_end': 2,
        'byte_start': 0,
        'byte_end': len(excerpt.encode()),
        'excerpt': excerpt,
    }
    response['bounds']['serialized_item_bytes'] = len(
        json.dumps(response['items'][0], separators=(',', ':')).encode()
    )
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
        "project_id": code_index_id_for_root(source_root),
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
        "inventory": {
            "digest": binding["inventory_digest"],
            "entries": [
                {
                    "path": "src/runtime.py",
                    "language": "python",
                    "exclusion": None,
                }
            ],
        },
    }
    identity_pointer = artifacts.write_body("snapshot-identity", identity)
    record = storage.attach_snapshot(
        record.run_id,
        inventory_digest=str(binding["inventory_digest"]),
        snapshot_artifact=identity_pointer,
        deadline_at=record.binding.deadline_at,
    )
    managed_execution_id = f"managed-{suffix}"
    grant = sign_grant(
        GrantBundle.model_validate(
            {
                "config_revision": 0,
                "deployment": {"token": "a" * 16, "fencing_epoch": 1},
                "schema_identity": {
                    "runner_protocol": 1,
                    "baseline_version": 1,
                    "baseline_checksum": "baseline",
                    "latest_version": 1,
                    "latest_checksum": "latest",
                    "assets_root_hash": "assets",
                },
                "principal": {
                    "kind": "tool_chat",
                    "machine_id": "fixture-machine",
                    "project_id": project_id,
                    "execution_id": managed_execution_id,
                    "session_id": "fixture-session",
                },
                "capabilities": {
                    "postgres": {
                        "mode": "direct",
                        "dsn": "postgresql://worker:super-secret@127.0.0.1/db",
                        "role_name": "worker",
                        "credential_generation": 1,
                        "valid_until": int(record.binding.deadline_at.timestamp()),
                    },
                    "falkordb": {"mode": "unavailable"},
                    "qdrant": {"mode": "unavailable"},
                    "embed": {"mode": "unavailable"},
                    "text_generate": {"mode": "daemon"},
                    "tool_chat": {"mode": "daemon"},
                    "vision_extract": {"mode": "unavailable"},
                    "audio_transcribe": {"mode": "unavailable"},
                    "broker_operations": [],
                },
                "issued_at": int(datetime.now(UTC).timestamp()),
                "expires_at": int(record.binding.deadline_at.timestamp()),
            }
        ),
        "fixture-signing-secret",
    )
    grant_path = state_root / f"grant-{suffix}.json"
    grant_path.parent.mkdir(parents=True, exist_ok=True)
    grant_path.write_bytes(grant.model_dump_canonical())
    runtime = SnapshotIndexRuntime(
        executable=executable,
        env={
            "GOBBY_AGENT_RUN_ID": managed_execution_id,
            "GOBBY_MACHINE_ID": "fixture-machine",
            "GOBBY_PROJECT_ID": project_id,
            "GOBBY_SESSION_ID": "fixture-session",
            "GOBBY_MANAGED_EXECUTION_BOOTSTRAP": str(grant_path),
        },
        managed_execution_id=managed_execution_id,
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

    source_reference = await admission.query(
        "search",
        {
            "search": {
                "lane": "content",
                "query": "token = runtime_token_reference",
                "paths": ["src"],
                "limit": 1000,
            }
        },
    )
    source_item = source_reference["items"][0]
    expected_excerpt = f"source = {Path.home()}/example.py\ntoken = runtime_token_reference"
    assert source_item["path"] == "src/runtime.py"
    assert source_item["excerpt"] == expected_excerpt
    assert source_item["excerpt_hash"] == hashlib.sha256(expected_excerpt.encode()).hexdigest()

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
    with pytest.raises(EvidenceAdmissionError, match="credential"):
        await admission.query(
            "search",
            {
                "search": {
                    "lane": "literal",
                    "query": "postgresql://worker:request-secret@127.0.0.1/db",
                    "paths": [],
                    "limit": 1000,
                }
            },
        )
    with pytest.raises(EvidenceAdmissionError, match="credential"):
        await admission.query(
            "search",
            {
                "search": {
                    "lane": "literal",
                    "query": "credential-response",
                    "paths": [],
                    "limit": 1000,
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

    checkpoint = [
        item.model_dump(mode="json") for item in storage.evidence_references(record.run_id)
    ]
    assert len(checkpoint) == 45
    assert len({item["invocation_id"] for item in checkpoint}) == 45
    assert any(item["status"] == "failed" for item in checkpoint)
    assert any(item["status"] == "denied" for item in checkpoint)
    assert any(item["status"] == "invalid_response" for item in checkpoint)
    assert all(item["usage"] is None for item in checkpoint)
    assert all(
        item["snapshot_inventory_digest"] == binding["inventory_digest"] for item in checkpoint
    )
    assert all(len(item["request_hash"]) == 64 for item in checkpoint)

    source_checkpoint = next(
        item for item in checkpoint if item["evidence_ids"] == ["src:source-reference-response"]
    )
    source_result = artifacts.read_body(source_checkpoint["result_artifact"])
    assert source_result["response"] == source_reference
    assert (
        source_checkpoint["response_hash"]
        == hashlib.sha256(
            json.dumps(
                source_reference,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )

    manifest = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["artifacts"]) == 92
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
    persisted_bytes = b"".join(
        path.read_bytes() for path in artifacts.run_root.rglob("*") if path.is_file()
    )
    assert b"request-secret" not in persisted_bytes
    assert b"successful-secret" not in persisted_bytes

    timed_checkpoint = [
        item.model_dump(mode="json")
        for item in timed_storage.evidence_references(timed_record.run_id)
    ]
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


def test_evidence_checkpoint_row_lock_obeys_deadline(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    project_id = str(sample_project["id"])
    execution_manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    storage, _artifacts, record, binding, _source, _runtime, _lifecycle = _persist_authority(
        execution_manager,
        project_id=project_id,
        state_root=tmp_path / "row-lock-state",
        executable=Path(sys.executable),
        argv_prefix=(),
        timeout_seconds=30,
        suffix="row-lock",
    )
    reference = EvidenceReference(
        invocation_id="held-row",
        operation="search",
        status="succeeded",
        invocation_artifact={"run_id": record.run_id},
        result_artifact={"run_id": record.run_id},
        evidence_ids=(),
        request_hash="a" * 64,
        response_hash="b" * 64,
        snapshot_inventory_digest=str(binding["inventory_digest"]),
    )

    with psycopg.connect(temp_db.conninfo) as holder:
        holder.execute(
            "SELECT id FROM pipeline_executions WHERE id = %s FOR UPDATE",
            (record.run_id,),
        )
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="checkpoint deadline"):
            storage.append_evidence_reference(
                record.run_id,
                reference,
                deadline_at=datetime.now(UTC) + timedelta(milliseconds=50),
            )
        assert time.monotonic() - started < 0.5

    assert storage.evidence_references(record.run_id) == []


@pytest.mark.asyncio
async def test_evidence_publication_timeout_records_only_terminal_timeout(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    project_id = str(sample_project["id"])
    execution_manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    script = tmp_path / "gcode_fixture.py"
    _write_gcode_fixture(script)
    storage, artifacts, record, _binding, _source, runtime, _lifecycle = _persist_authority(
        execution_manager,
        project_id=project_id,
        state_root=tmp_path / "publication-state",
        executable=Path(sys.executable),
        argv_prefix=("-S", str(script)),
        timeout_seconds=2,
        suffix="publication",
    )
    admission = EvidenceAdmission(
        run_id=record.run_id,
        runtime=runtime,
        permitted_operations={"search"},
        page_size=4096,
        artifacts=artifacts,
        storage=storage,
    )

    with psycopg.connect(temp_db.conninfo) as holder:
        holder.execute(
            "SELECT id FROM pipeline_executions WHERE id = %s FOR UPDATE",
            (record.run_id,),
        )

        async def release_after_main_deadline() -> None:
            delay = (record.binding.deadline_at - datetime.now(UTC)).total_seconds() + 0.1
            ready = asyncio.Event()
            asyncio.get_running_loop().call_later(max(0.1, delay), ready.set)
            await ready.wait()
            holder.rollback()

        release_task = asyncio.create_task(release_after_main_deadline())
        with pytest.raises(EvidenceAdmissionError, match="during publication"):
            await admission.query(
                "search",
                {"lane": "literal", "query": "publication-lock", "paths": [], "limit": 1000},
            )
        await release_task

    references = [
        item.model_dump(mode="json") for item in storage.evidence_references(record.run_id)
    ]
    assert [item["status"] for item in references] == ["timeout"]


class _SlowSpawnProcess:
    def __init__(self, request: dict[str, Any]) -> None:
        self.request = request
        self.returncode: int | None = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def communicate(self) -> tuple[bytes, bytes]:
        response = {
            "request": self.request,
            "request_fingerprint": __import__("hashlib")
            .sha256(json.dumps(self.request, separators=(",", ":")).encode())
            .hexdigest(),
            "binding": self.request["binding"],
            "contract": {
                "name": "gcode-evidence",
                "schema_version": 1,
                "tool": "gobby-code",
                "tool_version": "0.5.0",
                "lane": "literal_search",
            },
            "items": [{"kind": "source", "evidence_id": "src:slow-spawn"}],
            "complete": True,
            "completeness": "complete",
            "bounds": {
                "max_bytes": self.request["max_bytes"],
                "serialized_item_bytes": 10,
                "returned_items": 1,
                "total_items": 1,
                "result_limit": 1000,
            },
            "exclusions": [],
            "warnings": [],
            "continuation": None,
        }
        return json.dumps(response, separators=(",", ":")).encode(), b""


@pytest.mark.asyncio
async def test_evidence_cancellation_during_spawn_reaps_late_process(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = str(sample_project["id"])
    execution_manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    storage, artifacts, record, _binding, _source, runtime, _lifecycle = _persist_authority(
        execution_manager,
        project_id=project_id,
        state_root=tmp_path / "spawn-state",
        executable=Path(sys.executable),
        argv_prefix=(),
        timeout_seconds=30,
        suffix="spawn",
    )
    admission = EvidenceAdmission(
        run_id=record.run_id,
        runtime=runtime,
        permitted_operations={"search"},
        page_size=4096,
        artifacts=artifacts,
        storage=storage,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    spawned: list[_SlowSpawnProcess] = []

    async def slow_spawn(*argv: str, **_kwargs: object) -> _SlowSpawnProcess:
        request = json.loads(argv[argv.index("--request-json") + 1])
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        process = _SlowSpawnProcess(request)
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_spawn)
    task = asyncio.create_task(
        admission.query(
            "search",
            {"search": {"lane": "literal", "query": "slow-spawn", "paths": []}},
        )
    )
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(spawned) == 1
    assert spawned[0].killed is True
