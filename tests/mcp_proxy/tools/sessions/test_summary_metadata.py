from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.sessions import SessionSummaryConfig
from gobby.mcp_proxy.tools.sessions._summary_metadata import (
    compact_summary_metadata_matches,
)
from gobby.sessions.summarize import build_summary_source_context
from gobby.sessions.summary_refresh import digest_turn_count


@pytest.mark.asyncio
async def test_fresh_summary_matches_metadata_with_transcript_facts(tmp_path: Path) -> None:
    transcript = tmp_path / "facts.jsonl"
    records = [
        {"type": "user", "message": {"role": "user", "content": "Ground the summary"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "edit",
                        "name": "Write",
                        "input": {"file_path": "src/fact.py"},
                    },
                    {
                        "type": "tool_use",
                        "id": "task",
                        "name": "mcp__gobby__call_tool",
                        "input": {
                            "server_name": "gobby-tasks",
                            "tool_name": "link_commit",
                            "arguments": {"task_id": "#1", "commit_sha": "abc1234"},
                        },
                    },
                ],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    session = MagicMock()
    session.id = "metadata-session"
    session.source = "claude"
    session.transcript_path = str(transcript)
    session.terminal_context = None
    session.digest_markdown = "### Turn 1\nGrounded digest."
    session.last_turn_markdown = None
    session.last_assistant_content = None
    session.summary_markdown = (
        "## Current State\n\n"
        "The summary contains grounded transcript facts and enough detail for a reliable "
        "handoff.\n\n"
        "## Next Steps\n\nContinue from the grounded task, edit, and commit state."
    )
    manager = MagicMock()
    config = SessionSummaryConfig(prompt="Summary:\n{transcript_summary}")

    with (
        patch("gobby.sessions.summarize._enrich_git_context", new_callable=AsyncMock),
        patch("gobby.workflows.git_utils.get_file_changes", return_value="changes"),
        patch("gobby.workflows.git_utils.get_git_diff_summary", return_value="diff"),
    ):
        context = await build_summary_source_context(
            session,
            db=None,
            session_manager=manager,
            session_summary_config=config,
        )

    assert context is not None
    session.summary_source_context_hash = context.source_hash
    session.summary_digest_turn_count = digest_turn_count(session.digest_markdown)

    async def matches() -> bool:
        with (
            patch(
                "gobby.mcp_proxy.tools.sessions._summary_metadata.require_local_session_ownership"
            ),
            patch("gobby.sessions.summarize._enrich_git_context", new_callable=AsyncMock),
            patch("gobby.workflows.git_utils.get_file_changes", return_value="changes"),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value="diff"),
        ):
            return await compact_summary_metadata_matches(
                session=session,
                session_manager=manager,
                db=None,
                session_summary_config=config,
            )

    assert await matches() is True

    with transcript.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "read-only",
                                "content": "ignored successful output",
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
    assert await matches() is True

    with transcript.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "new-edit",
                                "name": "Write",
                                "input": {"file_path": "src/new-fact.py"},
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
    assert await matches() is False

    transcript.write_text("not-json\n" + transcript.read_text())
    assert await matches() is False
