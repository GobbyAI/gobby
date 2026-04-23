"""Shared fixtures for split storage session tests."""

import pytest


@pytest.fixture
def session_identity(sample_project: dict) -> dict[str, str]:
    """Common identity fields for registering storage sessions."""
    return {
        "external_id": "session-123",
        "machine_id": "machine-abc",
        "source": "claude",
        "project_id": sample_project["id"],
    }
