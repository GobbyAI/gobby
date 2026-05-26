"""Tests for agent completion subscriber helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.completion_subscribers import (
    completion_subscriber_lineage,
    subscribe_agent_completion,
)

pytestmark = pytest.mark.unit


def test_completion_subscriber_lineage_falls_back_on_unexpected_error() -> None:
    session_manager = MagicMock()
    session_manager.get.side_effect = RuntimeError("lineage failed")

    subscribers = completion_subscriber_lineage("child-session", session_manager)

    assert subscribers == ["child-session"]


def test_completion_subscriber_lineage_includes_parent_chain() -> None:
    root = SimpleNamespace(id="root-session", parent_session_id=None)
    child = SimpleNamespace(id="child-session", parent_session_id="root-session")
    session_manager = MagicMock()
    session_manager.get.side_effect = lambda session_id: {
        "root-session": root,
        "child-session": child,
    }[session_id]

    subscribers = completion_subscriber_lineage("child-session", session_manager)

    assert subscribers == ["root-session", "child-session"]


def test_subscribe_agent_completion_does_not_swallow_manager_constructor_errors() -> None:
    with patch(
        "gobby.storage.pipelines.LocalPipelineExecutionManager",
        side_effect=ValueError("bad project id"),
    ):
        with pytest.raises(ValueError, match="bad project id"):
            subscribe_agent_completion(
                completion_registry=None,
                run_id="run-1",
                subscriber_session_id="child-session",
                db=MagicMock(),
            )
