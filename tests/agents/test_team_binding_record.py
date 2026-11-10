"""A team admission record binds with or without reviewers and refuses unsupported reviewers."""

from __future__ import annotations

from dataclasses import fields

import pytest

from gobby.agents.team_admission_records import (
    TeamAdmissionRefused,
    TeamBinding,
    binding_from_record,
)

pytestmark = pytest.mark.unit


def test_team_binding_holds_no_run_binding_map() -> None:
    """No seat ever binds a run to a team, so the binding carries no primaries map to read."""
    assert "primaries" not in {field.name for field in fields(TeamBinding)}


def test_record_without_reviewers_still_binds() -> None:
    """Teams the daemon admitted before any seat was admitted carry no reviewers key."""
    binding = binding_from_record(
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
    assert binding.reviewers == []


def test_persisted_mapping_keeps_its_reviewer_names() -> None:
    """A record persisted in the name -> None shape still names its reviewers."""
    binding = binding_from_record(_record({"reviewer-a": None}))

    assert binding is not None
    assert binding.reviewers == ["reviewer-a"]


def test_unsupported_reviewers_value_refuses_the_record() -> None:
    """A reviewers value that is neither a list, a mapping nor null is refused, not read as none."""
    with pytest.raises(TeamAdmissionRefused):
        binding_from_record(_record("reviewer-a"))


def _record(reviewers: object) -> dict[str, object]:
    return {
        "team_id": "team-1",
        "task_id": "task-1",
        "project_id": "project-1",
        "branch": "epic/1/main",
        "base_branch": "0.5.0",
        "worktree_id": "worktree-1",
        "worktree_path": "/tmp/worktree-1",
        "reviewers": reviewers,
    }
