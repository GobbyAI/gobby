"""An in-process subagent's own compaction never resets its parent session.

Claude Code subagents share the parent's session id, so their SessionStart,
PreCompact and PostCompact hooks arrive addressed to the parent. Treating them
as the parent's context loss clears its loaded-instruction ledger and re-arms
every bootstrap block on the parent's next turn.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.hooks.subagent_boundary import subagent_boundary_signal

pytestmark = pytest.mark.unit

PARENT_TRANSCRIPT = "/home/u/.claude/projects/p/18b6d896.jsonl"
SUBAGENT_TRANSCRIPT = "/home/u/.claude/projects/p/18b6d896/subagents/agent-adfb45.jsonl"


def _event(
    event_type: HookEventType,
    data: dict[str, object],
    source: SessionSource = SessionSource.CLAUDE,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="18b6d896",
        source=source,
        timestamp=datetime.now(),
        data={"cwd": "/tmp", **data},
    )


def _dispatch(event: HookEvent) -> tuple[HookResponse, MagicMock, MagicMock]:
    """Dispatch with project resolution stubbed to skip, so reaching it is observable."""
    manager = MagicMock()
    with patch(
        "gobby.hooks.hook_manager.resolve_hook_project_context",
        return_value=SimpleNamespace(skipped=True, project_id=None, reason="test"),
    ) as resolve:
        response = HookManager._handle_after_daemon_ready(
            manager, event, BlockingEffectDeadline(123.0)
        )
    assert isinstance(response, HookResponse)
    return response, resolve, manager


@pytest.mark.parametrize(
    ("event_type", "data", "signal"),
    [
        (HookEventType.SESSION_START, {"source": "compact", "agent_id": "adfb45"}, "agent_id"),
        (
            HookEventType.SESSION_START,
            {"source": "compact", "transcript_path": SUBAGENT_TRANSCRIPT},
            "transcript_path",
        ),
        (HookEventType.PRE_COMPACT, {"trigger": "auto", "agent_id": "adfb45"}, "agent_id"),
        (
            HookEventType.PRE_COMPACT,
            {"trigger": "auto", "transcript_path": SUBAGENT_TRANSCRIPT},
            "transcript_path",
        ),
        (HookEventType.POST_COMPACT, {"trigger": "auto", "agent_id": "adfb45"}, "agent_id"),
    ],
)
def test_subagent_context_boundary_never_reaches_the_parent_session(
    event_type: HookEventType, data: dict[str, object], signal: str
) -> None:
    event = _event(event_type, data)

    response, resolve, manager = _dispatch(event)

    assert subagent_boundary_signal(event) == signal
    assert response.decision == "allow"
    assert response.context is None
    resolve.assert_not_called()
    manager._get_event_handler.assert_not_called()
    manager._evaluate_workflow_rules.assert_not_called()


@pytest.mark.parametrize(
    ("event_type", "data", "source"),
    [
        (
            HookEventType.SESSION_START,
            {"source": "compact", "transcript_path": PARENT_TRANSCRIPT},
            SessionSource.CLAUDE,
        ),
        (
            HookEventType.PRE_COMPACT,
            {"trigger": "manual", "transcript_path": PARENT_TRANSCRIPT},
            SessionSource.CLAUDE,
        ),
        # A subagent's tool call still belongs to the parent's turn.
        (HookEventType.BEFORE_TOOL, {"agent_id": "adfb45"}, SessionSource.CLAUDE),
        # Other providers bind child conversations by process identity instead.
        (HookEventType.SESSION_START, {"source": "compact", "agent_id": "x"}, SessionSource.GROK),
    ],
)
def test_parent_boundaries_and_subagent_tool_calls_still_resolve_the_session(
    event_type: HookEventType, data: dict[str, object], source: SessionSource
) -> None:
    event = _event(event_type, data, source)

    _, resolve, _ = _dispatch(event)

    assert subagent_boundary_signal(event) is None
    resolve.assert_called_once()
