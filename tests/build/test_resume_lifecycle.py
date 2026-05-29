"""Unit coverage for build lifecycle resume decisions."""

from __future__ import annotations

from gobby.build.resume_lifecycle import _resume_epic_workspace_refresh_required


def test_development_resume_only_needs_dispatcher_tick() -> None:
    assert _resume_epic_workspace_refresh_required("development") is False
    assert _resume_epic_workspace_refresh_required("planning") is False
    assert _resume_epic_workspace_refresh_required(None) is False


def test_delivery_resume_refreshes_epic_workspace() -> None:
    assert _resume_epic_workspace_refresh_required("holistic_qa") is True
    assert _resume_epic_workspace_refresh_required("pr") is True
    assert _resume_epic_workspace_refresh_required("merge") is True
