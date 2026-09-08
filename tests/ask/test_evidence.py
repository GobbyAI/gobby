from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.contracts import RetrievalMode
from gobby.ask.evidence import EvidenceAdmission, EvidenceAdmissionError
from gobby.ask.storage import AskRunStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager

pytestmark = pytest.mark.unit


def _write_gcode_fixture(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
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
print(json.dumps(response, separators=(',', ':')))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.mark.asyncio
async def test_durable_scoped_evidence_admission(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    project_id = str(sample_project["id"])
    execution_manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    execution = execution_manager.create_execution(
        pipeline_name="native-ask",
        inputs_json=json.dumps({"ask": {"binding": {"deadline_at": "pinned"}}}),
        definition_json=json.dumps({"name": "native-ask"}),
    )
    storage = AskRunStorage(execution_manager)
    artifacts = AskArtifactStore(tmp_path / "state", project_id, execution.id)
    executable = tmp_path / "gcode-fixture"
    _write_gcode_fixture(executable)
    source_root = tmp_path / "snapshot"
    source_root.mkdir()
    binding = {
        "project_id": project_id,
        "commit_oid": "a" * 40,
        "tree_oid": "b" * 40,
        "inventory_digest": "c" * 64,
        "commit": {
            "parent_oids": [],
            "comparison_parent_oid": "d" * 40,
            "comparison_kind": "empty_tree",
            "changed_paths_digest": "e" * 64,
            "changed_paths": [],
        },
    }
    admission = EvidenceAdmission(
        run_id=execution.id,
        source_root=source_root,
        snapshot_binding=binding,
        retrieval_mode=RetrievalMode.DETERMINISTIC,
        permitted_operations={"search", "read"},
        deadline_at=datetime.now(UTC) + timedelta(seconds=20),
        page_size=4096,
        executable=executable,
        env={"DATABASE_URL": "postgresql://worker:super-secret@127.0.0.1/db"},
        artifacts=artifacts,
        storage=storage,
    )

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
    with pytest.raises(EvidenceAdmissionError, match="sensitive path"):
        await admission.query(
            "read", {"read": {"range": {"path": ".env", "start_line": 1, "end_line": 1}}}
        )
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

    timed = EvidenceAdmission(
        run_id=execution.id,
        source_root=source_root,
        snapshot_binding=binding,
        retrieval_mode=RetrievalMode.DETERMINISTIC,
        permitted_operations={"search"},
        deadline_at=datetime.now(UTC) + timedelta(milliseconds=100),
        page_size=4096,
        executable=executable,
        env={},
        artifacts=artifacts,
        storage=storage,
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

    persisted = execution_manager.get_execution(execution.id)
    assert persisted is not None
    checkpoint = json.loads(persisted.outputs_json or "{}")["ask"]["evidence"]
    assert len(checkpoint) == 40
    assert len({item["invocation_id"] for item in checkpoint}) == 40
    assert any(item["status"] == "failed" for item in checkpoint)
    assert any(item["status"] == "denied" for item in checkpoint)
    assert any(item["status"] == "invalid_response" for item in checkpoint)
    assert any(item["status"] == "timeout" for item in checkpoint)
    assert all(item["usage"] is None for item in checkpoint)
    assert all(
        item["snapshot_inventory_digest"] == binding["inventory_digest"] for item in checkpoint
    )
    assert all(len(item["request_hash"]) == 64 for item in checkpoint)

    manifest = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["artifacts"]) == 80
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
