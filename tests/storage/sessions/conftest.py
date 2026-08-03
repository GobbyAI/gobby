"""Shared fixtures for split storage session tests."""

import pytest


@pytest.fixture
def session_identity(sample_project: dict) -> dict[str, str]:
    """Common identity fields for registering storage sessions."""
    return {
        "external_id": "session-123",
        "machine_id": "20000000-0000-4000-8000-000000000005",
        "source": "claude",
        "project_id": sample_project["id"],
    }
