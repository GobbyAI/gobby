from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from gobby.hooks.project_checkout_ingress import register_cwd_marker_checkout
from gobby.hooks.project_context import ProjectIdResolver
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager, resolve_operation_root
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from tests.fixtures.isolated_checkout import (
    insert_isolated_machine,
    insert_overlay,
    patch_local_machine_id,
    write_project_marker,
)
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.integration


def _request_as(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    machine_id: str,
    method: str,
    path: str,
    **kwargs: Any,
) -> Response:
    patch_local_machine_id(monkeypatch, machine_id)
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", machine_id)
    return client.request(method, path, **kwargs)


def _seed_index_state(
    db: HubDatabase,
    machine_id: str,
    project_id: str,
    root_path: str,
    *,
    file_path: str,
) -> None:
    content_hash = f"sha256:{machine_id}"
    file_id = str(uuid.uuid5(uuid.UUID(project_id), f"{file_path}:{content_hash}"))
    db.execute(
        "INSERT INTO code_indexed_projects (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
        (project_id,),
    )
    db.execute(
        """
        INSERT INTO code_indexed_files (
            id, project_id, file_path, language, content_hash,
            symbol_count, byte_size, graph_synced, vectors_synced
        ) VALUES (%s, %s, %s, 'python', %s, 1, 10, true, true)
        """,
        (file_id, project_id, file_path, content_hash),
    )
    db.execute(
        """
        INSERT INTO code_indexed_project_states (
            machine_id, project_id, root_path, total_files, total_symbols
        ) VALUES (%s, %s, %s, 1, 1)
        """,
        (machine_id, project_id, root_path),
    )
    db.execute(
        """
        INSERT INTO code_indexed_file_states (
            machine_id, project_id, file_path, content_hash
        ) VALUES (%s, %s, %s, %s)
        """,
        (machine_id, project_id, file_path, content_hash),
    )


def _index_root(db: HubDatabase, machine_id: str, project_id: str) -> str | None:
    row = db.fetchone(
        """
        SELECT root_path
        FROM code_indexed_project_states
        WHERE machine_id = %s AND project_id = %s
        """,
        (machine_id, project_id),
    )
    return None if row is None else str(row["root_path"])


def _indexed_files(db: HubDatabase, machine_id: str, project_id: str) -> list[str]:
    rows = db.fetchall(
        """
        SELECT file_path
        FROM code_indexed_file_states
        WHERE machine_id = %s AND project_id = %s
        ORDER BY file_path
        """,
        (machine_id, project_id),
    )
    return [str(row["file_path"]) for row in rows]


def test_two_machine_checkout_identity_composes_without_cross_machine_leaks(
    temp_db: HubDatabase,
    project_manager: LocalProjectManager,
    session_manager: SessionManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = project_manager.create(name=f"checkout-identity-{uuid.uuid4()}")
    machine_a = insert_isolated_machine(temp_db)
    machine_b = insert_isolated_machine(temp_db)
    root_a = tmp_path / "machine-a"
    root_b = tmp_path / "machine-b"
    overlay_a = tmp_path / "machine-a-worktree"
    for root in (root_a, root_b, overlay_a):
        root.mkdir()
        write_project_marker(root, project_id=project.id, name=project.name)

    checkouts = LocalProjectCheckoutManager(temp_db)
    checkouts.register(machine_b, project.id, str(root_b))
    _seed_index_state(
        temp_db,
        machine_b,
        project.id,
        str(root_b),
        file_path="src/machine_b.py",
    )
    insert_overlay(
        temp_db,
        project_id=project.id,
        machine_id=machine_a,
        path=str(overlay_a),
        kind="worktree",
    )

    patch_local_machine_id(monkeypatch, machine_a)
    resolver = ProjectIdResolver(
        ensure_project_in_db=lambda context: register_cwd_marker_checkout(temp_db, context)
    )
    assert resolver.resolve(None, str(overlay_a)) == project.id
    assert resolve_operation_root(
        temp_db,
        project.id,
        machine_a,
        overlay_path=str(overlay_a),
    ) == str(overlay_a)
    assert checkouts.get(machine_a, project.id) is None

    server = create_http_server(
        session_manager=session_manager,
        database=session_manager.db,
    )
    with TestClient(server.app) as client:
        empty_a = _request_as(
            monkeypatch,
            client,
            machine_a,
            "GET",
            f"/api/projects/{project.id}",
        )
        visible_b = _request_as(
            monkeypatch,
            client,
            machine_b,
            "GET",
            f"/api/projects/{project.id}",
        )
        assert empty_a.status_code == 200
        assert empty_a.json()["checkout"] is None
        assert str(root_b) not in empty_a.text
        assert visible_b.status_code == 200
        assert visible_b.json()["checkout"] == {
            "machine_id": machine_b,
            "root_path": str(root_b),
        }
        assert str(root_a) not in visible_b.text

        overlay_register = _request_as(
            monkeypatch,
            client,
            machine_a,
            "POST",
            f"/api/projects/{project.id}/checkouts",
            json={"root_path": str(overlay_a)},
        )
        assert overlay_register.status_code == 409
        assert "OverlayRegistrationRejectedError" in overlay_register.text
        assert checkouts.get(machine_a, project.id) is None

        foreign_rebind = _request_as(
            monkeypatch,
            client,
            machine_a,
            "POST",
            f"/api/projects/{project.id}/checkouts/{machine_b}/rebind",
            json={"root_path": str(root_a)},
        )
        assert foreign_rebind.status_code == 409
        assert "MachineOwnershipMismatchError" in foreign_rebind.text
        assert checkouts.get(machine_a, project.id) is None

        _seed_index_state(
            temp_db,
            machine_a,
            project.id,
            "/stale/machine-a",
            file_path="src/machine_a.py",
        )
        rebound_a = _request_as(
            monkeypatch,
            client,
            machine_a,
            "POST",
            f"/api/projects/{project.id}/checkouts/{machine_a}/rebind",
            json={"root_path": str(root_a)},
        )
        assert rebound_a.status_code == 200
        assert rebound_a.json()["checkout"] == {
            "machine_id": machine_a,
            "root_path": str(root_a),
        }

        visible_a = _request_as(
            monkeypatch,
            client,
            machine_a,
            "GET",
            f"/api/projects/{project.id}",
        )
        visible_b_after = _request_as(
            monkeypatch,
            client,
            machine_b,
            "GET",
            f"/api/projects/{project.id}",
        )

    assert visible_a.json()["checkout"] == {
        "machine_id": machine_a,
        "root_path": str(root_a),
    }
    assert str(root_b) not in visible_a.text
    assert visible_b_after.json()["checkout"] == {
        "machine_id": machine_b,
        "root_path": str(root_b),
    }
    assert str(root_a) not in visible_b_after.text

    checkout_rows = temp_db.fetchall(
        """
        SELECT machine_id, root_path
        FROM project_checkouts
        WHERE project_id = %s
        ORDER BY machine_id
        """,
        (project.id,),
    )
    assert {str(row["machine_id"]): str(row["root_path"]) for row in checkout_rows} == {
        machine_a: str(root_a),
        machine_b: str(root_b),
    }
    assert _index_root(temp_db, machine_a, project.id) is None
    assert _indexed_files(temp_db, machine_a, project.id) == []
    assert _index_root(temp_db, machine_b, project.id) == str(root_b)
    assert _indexed_files(temp_db, machine_b, project.id) == ["src/machine_b.py"]
