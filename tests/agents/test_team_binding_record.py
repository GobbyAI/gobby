"""A team record written before its optional maps exist still binds."""

from __future__ import annotations

import pytest

from gobby.agents.team_lifecycle import _binding

pytestmark = pytest.mark.unit


def test_record_without_reviewers_or_primaries_still_binds() -> None:
    """Teams the daemon admitted before any seat bound carry no reviewers or primaries key."""
    binding = _binding(
        {
            "team_id": "team-1",
            "task_id": "task-1",
            "project_id": "project-1",
            "branch": "epic/1/main",
            "base_branch": "0.5.0",
            "worktree_id": "worktree-1",
            "worktree_path": "/tmp/worktree-1",
        }
    )

    assert binding is not None
    assert binding.reviewers == {}
    assert binding.primaries == {}
