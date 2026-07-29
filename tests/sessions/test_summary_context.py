"""Tests for session-scoped summary prompt context."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.sessions.analyzer import HandoffContext
from gobby.sessions.summary_context import (
    _build_summary_prompt_context,
    _scoped_git_status,
    _source_hash_payload,
)
from gobby.sessions.summary_refresh import source_context_hash
from gobby.sessions.workspace_context import enrich_git_context

pytestmark = pytest.mark.unit

_SESSION_PATH = "/workspace/reviewer/transcript.jsonl"
_REPO_NOISE_PATH = "/workspace/shared/unrelated.py"


def _session() -> MagicMock:
    session = MagicMock()
    session.id = "reviewer-session"
    session.source = "codex"
    session.transcript_path = _SESSION_PATH
    session.digest_markdown = "### Turn 1\nReviewed the plan."
    session.last_turn_markdown = "Review the plan."
    session.last_assistant_content = "Review complete."
    return session


async def _summary_context(
    *,
    handoff_ctx: HandoffContext,
    file_changes: str,
    git_diff_summary: str,
    run_db: Callable[..., Awaitable[object]] | None = None,
) -> dict[str, object]:
    manager = MagicMock()
    manager.db = None
    with (
        patch(
            "gobby.workflows.git_utils.get_file_changes",
            return_value=file_changes,
        ),
        patch(
            "gobby.workflows.git_utils.get_git_diff_summary",
            return_value=git_diff_summary,
        ),
    ):
        return await _build_summary_prompt_context(
            session=_session(),
            turns=[],
            handoff_ctx=handoff_ctx,
            db=None,
            session_manager=manager,
            project_path="/workspace/shared",
            run_db=run_db,
        )


@pytest.mark.asyncio
async def test_no_edit_session_excludes_repo_noise() -> None:
    handoff_ctx = HandoffContext()

    with patch(
        "gobby.sessions.workspace_context.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    ) as create_subprocess:
        await enrich_git_context(handoff_ctx, Path("/workspace/shared"))

    context = await _summary_context(
        handoff_ctx=handoff_ctx,
        file_changes=f"Untracked: {_REPO_NOISE_PATH}",
        git_diff_summary=f"diff --git a{_REPO_NOISE_PATH} b{_REPO_NOISE_PATH}",
    )

    assert create_subprocess.await_count == 0
    assert _REPO_NOISE_PATH not in str(context)


@pytest.mark.asyncio
async def test_hash_stable_across_unrelated_commits() -> None:
    first_handoff = HandoffContext(
        git_status=f" M {_REPO_NOISE_PATH}",
        git_commits=[{"hash": "first", "message": "unrelated first commit"}],
    )
    second_handoff = HandoffContext(
        git_status=f" M {_REPO_NOISE_PATH}",
        git_commits=[{"hash": "second", "message": "unrelated second commit"}],
    )
    first_context = await _summary_context(
        handoff_ctx=first_handoff,
        file_changes=f"Untracked: {_REPO_NOISE_PATH}",
        git_diff_summary="first unrelated diff",
    )
    second_context = await _summary_context(
        handoff_ctx=second_handoff,
        file_changes=f"Untracked: {_REPO_NOISE_PATH}",
        git_diff_summary="second unrelated diff",
    )

    first_hash = source_context_hash(
        _source_hash_payload(
            session=_session(),
            digest_markdown="### Turn 1\nReviewed the plan.",
            summary_context=first_context,
            prompt_template="Summary",
        )
    )
    second_hash = source_context_hash(
        _source_hash_payload(
            session=_session(),
            digest_markdown="### Turn 1\nReviewed the plan.",
            summary_context=second_context,
            prompt_template="Summary",
        )
    )

    assert first_hash == second_hash


def test_scoped_git_status_handles_quoted_and_renamed_paths() -> None:
    status = ' M "dir/file\\tname.py"\nR  old.py -> new.py\n M unrelated.py'

    result = _scoped_git_status(
        status,
        ("dir/file\tname.py", "new.py"),
    )

    assert result == ' M "dir/file\\tname.py"\nR  old.py -> new.py'


@pytest.mark.asyncio
async def test_git_context_helpers_run_through_database_executor() -> None:
    calls: list[tuple[Callable[..., object], dict[str, object]]] = []

    async def run_db(
        func: Callable[..., object],
        *_args: object,
        **kwargs: object,
    ) -> object:
        calls.append((func, kwargs))
        return func(**kwargs)

    context = await _summary_context(
        handoff_ctx=HandoffContext(
            files_modified=["/workspace/shared/dir/file.py"],
            git_status=" M dir/file.py",
        ),
        file_changes="file changes",
        git_diff_summary="diff summary",
        run_db=run_db,
    )

    assert context["file_changes"] == "file changes"
    assert context["git_diff_summary"] == "diff summary"
    assert len(calls) == 2
    assert all(call_kwargs["paths"] == ("dir/file.py",) for _, call_kwargs in calls)
