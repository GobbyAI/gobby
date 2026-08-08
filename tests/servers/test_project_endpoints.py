"""Tests for project-aware HTTP endpoint behavior."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


class TestProjectResolutionEndpoints:
    """Tests for project-aware session endpoint behavior."""

    def test_register_session(
        self,
        client: TestClient,
        test_project: dict,
        temp_dir: Path,
    ) -> None:
        """Test session registration endpoint."""
        with patch(
            "gobby.utils.machine_id.get_machine_id",
            return_value="21000000-0000-4000-8000-000000000002",
        ):
            response = client.post(
                "/api/sessions/register",
                json={
                    "external_id": "test-cli-key",
                    "machine_id": "21000000-0000-4000-8000-000000000002",
                    "source": "claude",
                    "cwd": str(temp_dir),
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "registered"
        assert data["external_id"] == "test-cli-key"
        assert "id" in data

    def test_register_session_with_all_fields(
        self,
        client: TestClient,
        test_project: dict,
        temp_dir: Path,
    ) -> None:
        """Test session registration with all optional fields."""
        response = client.post(
            "/api/sessions/register",
            json={
                "external_id": "full-cli-key",
                "machine_id": LOCAL_MACHINE_ID,
                "source": "Claude Code",
                "project_id": test_project["id"],
                "title": "Full Session",
                "transcript_path": "/path/to/transcript.jsonl",
                "git_branch": "feature/test",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["external_id"] == "full-cli-key"
        assert data["machine_id"] == LOCAL_MACHINE_ID

    def test_find_current_session(
        self,
        client: TestClient,
        session_storage: SessionManager,
        test_project: dict,
    ) -> None:
        """Test finding current session by composite key."""
        session = session_storage.register(
            external_id="find-current",
            machine_id=LOCAL_MACHINE_ID,
            source="qwen",
            project_id=test_project["id"],
        )

        response = client.post(
            "/api/sessions/find_current",
            json={
                "external_id": "find-current",
                "machine_id": LOCAL_MACHINE_ID,
                "source": "qwen",
                "project_id": test_project["id"],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["id"] == session.id

    def test_find_current_session_not_found(self, client: TestClient, test_project: dict) -> None:
        """Test finding nonexistent current session."""
        response = client.post(
            "/api/sessions/find_current",
            json={
                "external_id": "nonexistent",
                "machine_id": "21000000-0000-4000-8000-000000000003",
                "source": "claude",
                "project_id": test_project["id"],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session"] is None

    def test_find_current_session_missing_project_id(self, client: TestClient) -> None:
        """Test find_current without project_id or cwd returns 400."""
        response = client.post(
            "/api/sessions/find_current",
            json={
                "external_id": "test",
                "machine_id": "21000000-0000-4000-8000-000000000003",
                "source": "claude",
            },
        )

        assert response.status_code == 400
        assert "project_id or cwd" in response.json()["detail"]

    def test_register_then_get_persistence(
        self,
        client: TestClient,
        test_project: dict,
        temp_dir: Path,
    ) -> None:
        """Test that session registered via HTTP is retrievable via get endpoint."""
        # Register via HTTP endpoint
        with patch("gobby.utils.machine_id.get_machine_id", return_value=LOCAL_MACHINE_ID):
            register_response = client.post(
                "/api/sessions/register",
                json={
                    "external_id": "persist-test-key",
                    "machine_id": "21000000-0000-4000-8000-000000000002",
                    "source": "claude",
                    "cwd": str(temp_dir),
                    "title": "Persistence Test Session",
                },
            )

        assert register_response.status_code == 200
        register_data = register_response.json()
        session_id = register_data["id"]

        # Retrieve via GET endpoint
        get_response = client.get(f"/api/sessions/{session_id}")
        assert get_response.status_code == 200

        get_data = get_response.json()
        assert get_data["status"] == "success"
        assert get_data["session"]["external_id"] == "persist-test-key"
        assert get_data["session"]["id"] == session_id
        assert get_data["session"]["title"] == "Persistence Test Session"

    def test_register_with_invalid_project_path(
        self,
        client: TestClient,
        temp_dir: Path,
    ) -> None:
        """Test registration with non-existent project path returns 400.

        When cwd points to a path without .gobby/project.json, _resolve_project_id
        raises ValueError, which is caught and converted to HTTP 400 Bad Request.
        """
        with patch(
            "gobby.utils.machine_id.get_machine_id",
            return_value="21000000-0000-4000-8000-000000000002",
        ):
            response = client.post(
                "/api/sessions/register",
                json={
                    "external_id": "invalid-path-test",
                    "machine_id": "21000000-0000-4000-8000-000000000002",
                    "source": "claude",
                    "cwd": "/nonexistent/path/that/does/not/exist",
                },
            )
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "No .gobby/project.json found" in data["detail"]
