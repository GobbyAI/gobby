"""Pinned projection recovery is scheduled without child-owned file selectors."""

from pathlib import Path
from uuid import uuid4

import pytest

from gobby.code_index.models import IndexedFile, IndexedProject, IndexWriteMode
from gobby.code_index.storage import CodeIndexStorage
from gobby.code_index.sync_worker import _sync_pass
from gobby.config.code_index import CodeIndexConfig
from gobby.utils.machine_id import require_machine_id
from tests.code_index.test_sync_worker import RecordingGcodeGateway

pytestmark = pytest.mark.integration

PIN_PATH = "src/pinned.rs"


def _file(
    storage: CodeIndexStorage,
    project_id: str,
    root: Path,
    content_hash: str,
    *,
    language: str = "rust",
    synced: bool = False,
) -> IndexedFile:
    file = IndexedFile(
        id=IndexedFile.make_id(project_id, PIN_PATH, content_hash),
        project_id=project_id,
        file_path=PIN_PATH,
        language=language,
        content_hash=content_hash,
        symbol_count=1,
        graph_synced=synced,
        vectors_synced=synced,
    )
    storage.upsert_file(file, root_path=str(root), mode=IndexWriteMode.OVERLAY)
    return file


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,expected_graph,expected_vector",
    [
        ("moved", True, True),
        ("source-still-current", False, False),
        ("owned-shadow", False, False),
        ("tombstone-shadow", False, False),
        ("graph-projected", False, True),
        ("vector-projected", True, False),
        ("recent-attempt", False, False),
        ("foreign-machine", False, False),
        ("graph-disabled", False, True),
        ("vector-disabled", True, False),
    ],
)
async def test_worker_schedules_pinned_recovery(
    code_storage: CodeIndexStorage,
    tmp_path: Path,
    case: str,
    expected_graph: bool,
    expected_vector: bool,
) -> None:
    source_id, overlay_id = str(uuid4()), str(uuid4())
    source_root = tmp_path / "unavailable-source-checkout"
    overlay_root = tmp_path / "overlay"
    overlay_root.mkdir()
    for project_id, root in [(source_id, source_root), (overlay_id, overlay_root)]:
        code_storage.upsert_project_stats(
            IndexedProject(id=project_id, root_path=str(root)), mode=IndexWriteMode.OVERLAY
        )
    source_file = _file(code_storage, source_id, source_root, "pinned-version")
    if case != "source-still-current":
        _file(code_storage, source_id, source_root, "new-source-version")
    if case in {"owned-shadow", "tombstone-shadow"}:
        _file(
            code_storage,
            overlay_id,
            overlay_root,
            "__gcode_tombstone__" if case == "tombstone-shadow" else "child-version",
            language="__gcode_deleted__" if case == "tombstone-shadow" else "rust",
            synced=True,
        )
    pin_machine = str(uuid4()) if case == "foreign-machine" else require_machine_id()
    with code_storage.db.transaction() as conn:
        conn.execute(
            """INSERT INTO code_overlay_pins (
                machine_id, overlay_project_id, file_path, source_project_id, content_hash,
                graph_projected, vector_projected, graph_attempted_at, vector_attempted_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s,
                CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END,
                CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END)""",
            (
                pin_machine,
                overlay_id,
                PIN_PATH,
                source_id,
                source_file.content_hash,
                case == "graph-projected",
                case == "vector-projected",
                case == "recent-attempt",
                case == "recent-attempt",
            ),
        )
    source_sql = """SELECT graph_synced, vectors_synced,
                    graph_sync_attempted_at, vector_sync_attempted_at
                    FROM code_indexed_files WHERE id = %s"""
    before = code_storage.db.fetchone(source_sql, (source_file.id,))
    gateway = RecordingGcodeGateway()

    await _sync_pass(
        storage=code_storage,
        gcode_gateway=gateway,
        config=CodeIndexConfig(
            graph_enabled=case != "graph-disabled",
            embedding_enabled=case != "vector-disabled",
        ),
        batch_size=10,
    )

    assert gateway.synced_files == ([(overlay_root, PIN_PATH)] if expected_graph else [])
    assert gateway.vector_synced_files == ([(overlay_root, PIN_PATH)] if expected_vector else [])
    assert code_storage.db.fetchone(source_sql, (source_file.id,)) == before
