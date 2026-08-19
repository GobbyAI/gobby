from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.events import SessionSource
from gobby.mcp_proxy.services.session_context import (
    resolve_tool_event_context,
    should_synthesize_direct_after_tool,
)

pytestmark = pytest.mark.unit


class _Service:
    def __init__(self, hook_manager: object) -> None:
        self._hook_manager = hook_manager

    def _resolve_hook_manager(self) -> object:
        return self._hook_manager


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (SessionSource.CODEX, False),
        (SessionSource.CLAUDE, False),
        (SessionSource.QWEN, False),
        (SessionSource.DROID, False),
        (SessionSource.GROK, False),
        (SessionSource.AGY, False),
        (SessionSource.PIPELINE, True),
        (SessionSource.UNKNOWN, True),
        ("unsupported", True),
        (None, True),
    ],
)
def test_should_synthesize_direct_after_tool(
    source: SessionSource | str | None,
    expected: bool,
) -> None:
    assert should_synthesize_direct_after_tool(source) is expected


def test_resolve_tool_event_context_tolerates_unsupported_source() -> None:
    session = SimpleNamespace(
        source="unsupported",
        project_id="project-1",
        external_id="external-1",
    )
    session_storage = MagicMock()
    session_storage.get.return_value = session
    hook_manager = SimpleNamespace(_session_manager=session_storage)

    with patch(
        "gobby.utils.project_context.get_project_context",
        return_value={"project_path": "/tmp/repo", "id": "project-ctx"},
    ):
        (
            _hook_manager,
            _session_storage,
            returned_session,
            source,
            metadata,
            cwd,
            project_id,
        ) = resolve_tool_event_context(_Service(hook_manager), "session-1")

    assert returned_session is session
    assert source is SessionSource.UNKNOWN
    assert metadata["external_id"] == "external-1"
    assert cwd == "/tmp/repo"
    assert project_id == "project-ctx"
