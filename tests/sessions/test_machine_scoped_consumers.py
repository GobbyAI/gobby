from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from fastapi import APIRouter, HTTPException
from fastapi.routing import APIRoute

from gobby.agents.watchdog.transcript_resolver import WatchdogTranscriptResolver
from gobby.cli.sessions import sessions
from gobby.hooks.event_handlers._session_start.transcripts import derive_transcript_path
from gobby.mcp_proxy.tools.sessions._factory import create_session_messages_registry
from gobby.servers.http import HTTPServer
from gobby.servers.routes.sessions.analytics import register_analytics_routes
from gobby.sessions.summarize import generate_session_summaries
from gobby.sessions.transcript_paths import find_transcript_on_disk
from gobby.sessions.transcript_reader import TranscriptReader
from gobby.storage.session_models import Session
from gobby.storage.sessions._transcript import _TranscriptMixin

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
REMOTE_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


def _remote_session() -> Session:
    now = datetime.now(UTC)
    return Session(
        id="31000000-0000-4000-8000-000000000001",
        external_id="remote-external",
        machine_id=REMOTE_MACHINE_ID,
        source="codex",
        project_id="41000000-0000-4000-8000-000000000001",
        title=None,
        status="expired",
        transcript_path="/remote/transcript.jsonl",
        summary_path=None,
        summary_markdown="# Summary\n\nRemote summary",
        git_branch=None,
        parent_session_id=None,
        created_at=now,
        updated_at=now,
        handoff_markdown="## Turn 1\nRemote turn",
        summary_source_context_hash="stored-hash",
    )


async def _run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


@pytest.mark.asyncio
async def test_remote_sessions_skipped() -> None:
    session = _remote_session()
    db = MagicMock()
    db.fetchall.return_value = []
    storage = SimpleNamespace(db=db)

    with patch(
        "gobby.storage.sessions._transcript.get_machine_id",
        return_value=LOCAL_MACHINE_ID,
    ):
        pending = _TranscriptMixin.get_pending_transcript_sessions(storage)

    assert pending == []
    query, params = db.fetchall.call_args.args
    assert "machine_id = %s" in query
    assert params == (LOCAL_MACHINE_ID, 10)

    manager = MagicMock()
    manager.get.return_value = session
    manager.mark_transcript_processed = MagicMock()
    with (
        patch(
            "gobby.sessions.machine_scope.get_machine_id",
            return_value=LOCAL_MACHINE_ID,
        ),
        patch(
            "gobby.sessions.summarize.Path.exists",
            side_effect=AssertionError("remote transcript was probed"),
        ),
    ):
        result = await generate_session_summaries(
            session.id,
            manager,
            run_db=_run_db,
        )

    assert result["success"] is False
    assert "remote machine" in result["error"]
    manager.mark_transcript_processed.assert_not_called()


def test_fallback_scan_refuses_remote_sessions() -> None:
    handler = SimpleNamespace(
        _find_qwen_transcript=MagicMock(
            side_effect=AssertionError("remote qwen transcript was scanned")
        )
    )
    with patch(
        "gobby.sessions.transcript_paths.Path.home",
        side_effect=AssertionError("remote home directory was scanned"),
    ):
        assert (
            find_transcript_on_disk(
                "codex",
                "remote-external",
                owner_machine_id=REMOTE_MACHINE_ID,
                local_machine_id=LOCAL_MACHINE_ID,
            )
            is None
        )

    assert (
        derive_transcript_path(
            handler,
            "qwen",
            {},
            "remote-external",
            owner_machine_id=REMOTE_MACHINE_ID,
            local_machine_id=LOCAL_MACHINE_ID,
        )
        is None
    )
    handler._find_qwen_transcript.assert_not_called()


@pytest.mark.asyncio
async def test_on_demand_summary_refuses_remote_sessions() -> None:
    session = _remote_session()
    manager = MagicMock()
    manager.get.return_value = session

    with (
        patch(
            "gobby.sessions.machine_scope.get_machine_id",
            return_value=LOCAL_MACHINE_ID,
        ),
        patch(
            "gobby.sessions.summarize.Path.exists",
            side_effect=AssertionError("remote transcript was probed"),
        ),
    ):
        result = await generate_session_summaries(
            session_manager=manager,
            session_id=session.id,
            llm_service=MagicMock(),
            session_summary_config=MagicMock(),
        )

    assert result is not None
    assert "remote machine" in result["error"]

    router = APIRouter()
    server = SimpleNamespace(
        session_manager=manager,
        llm_service=MagicMock(),
        config=SimpleNamespace(session_summary=MagicMock()),
    )
    register_analytics_routes(router, cast(HTTPServer, server), MagicMock(), AsyncMock())
    route = next(
        route
        for route in router.routes
        if getattr(route, "path", "") == "/{session_id}/generate-summary"
    )
    assert isinstance(route, APIRoute)
    endpoint = route.endpoint
    with (
        patch(
            "gobby.sessions.machine_scope.get_machine_id",
            return_value=LOCAL_MACHINE_ID,
        ),
        patch("gobby.sessions.transcripts.get_parser") as get_parser,
        pytest.raises(HTTPException) as exc_info,
    ):
        await endpoint(session.id)

    assert exc_info.value.status_code == 403
    assert "remote machine" in str(exc_info.value.detail)
    get_parser.assert_not_called()


@pytest.mark.asyncio
async def test_reader_and_watchdog_refuse_remote_fallback() -> None:
    session = _remote_session()
    manager = MagicMock()
    manager.get.return_value = session
    reader = TranscriptReader(manager)
    resolver = WatchdogTranscriptResolver()
    resolver._path_cache[("run-1", session.source, session.external_id)] = "/cached/path"

    with (
        patch(
            "gobby.sessions.machine_scope.get_machine_id",
            return_value=LOCAL_MACHINE_ID,
        ),
        patch(
            "gobby.sessions.transcript_reader.os.path.isfile",
            side_effect=AssertionError("remote transcript was statted"),
        ),
        pytest.raises(PermissionError, match="remote machine"),
    ):
        await reader.get_messages(session.id)

    with (
        patch(
            "gobby.sessions.machine_scope.get_machine_id",
            return_value=LOCAL_MACHINE_ID,
        ),
        patch(
            "gobby.agents.watchdog.transcript_resolver.os.path.isfile",
            side_effect=AssertionError("remote watchdog path was statted"),
        ),
    ):
        assert await resolver.resolve(session, run_id="run-1") is None

    manager.update.assert_not_called()
    assert resolver._path_cache == {("run-1", session.source, session.external_id): "/cached/path"}


@pytest.mark.asyncio
async def test_session_filesystem_surfaces_refuse_remote_owner() -> None:
    session = _remote_session()
    manager = MagicMock()
    manager.get.return_value = session
    manager.db = None

    with (
        patch(
            "gobby.sessions.machine_scope.get_machine_id",
            return_value=LOCAL_MACHINE_ID,
        ),
        patch(
            "gobby.cli.sessions.session_manager_context",
            return_value=nullcontext(manager),
        ),
        patch("gobby.cli.sessions.resolve_session_id", return_value=session.id),
        patch(
            "gobby.cli.sessions.Path.exists",
            side_effect=AssertionError("remote transcript was probed"),
        ),
        patch("builtins.open", side_effect=AssertionError("remote transcript was opened")),
        patch("subprocess.run", side_effect=AssertionError("git was invoked")),
    ):
        cli_result = CliRunner().invoke(
            sessions,
            ["summarize", "-s", session.id, "--output", "db"],
        )

    assert cli_result.exit_code == 1
    assert "remote machine" in cli_result.output

    registry = create_session_messages_registry(session_manager=manager, db=None)
    with (
        patch(
            "gobby.sessions.machine_scope.get_machine_id",
            return_value=LOCAL_MACHINE_ID,
        ),
        patch("subprocess.run", side_effect=AssertionError("git was invoked")),
    ):
        commits_result = await registry.call(
            "get_session_commits",
            {"session_id": session.id},
        )

    assert commits_result["success"] is False
    assert "remote machine" in commits_result["error"]
