"""Tests for sessions/summarize.py — shared session summary generation."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.sessions import SessionSummaryConfig
from gobby.sessions.analyzer import HandoffContext
from gobby.sessions.summarize import (
    TRANSCRIPT_FALLBACK_MAX_CHARS,
    TRANSCRIPT_FALLBACK_MAX_TURNS,
    _build_summary_prompt_context,
    _digest_markdown_for_summary,
    _generate_delta_summary,
    _generate_full_summary,
    _source_hash_payload,
    _SummaryCoreResult,
    generate_session_summaries,
)
from gobby.storage.executor import DatabaseExecutor
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager

VALID_SUMMARY = """## Current State

The focused summary-generation behavior completed successfully with enough detail to provide a
useful handoff to the next session.

## Next Steps

Continue from the captured session state and complete the active task.
"""

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture(autouse=True)
def _local_session_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.sessions.summarize.require_local_session_ownership",
        lambda _session: "local-machine",
    )


def _make_session(
    session_id: str = "sess-1",
    transcript_path: str | None = None,
    source: str = "claude",
    summary_markdown: str | None = None,
    digest_markdown: str | None = None,
    last_turn_markdown: str | None = None,
    last_assistant_content: str | None = None,
) -> MagicMock:
    session = MagicMock()
    session.id = session_id
    session.transcript_path = transcript_path
    session.source = source
    session.summary_markdown = summary_markdown
    session.digest_markdown = digest_markdown
    session.last_turn_markdown = last_turn_markdown
    session.last_assistant_content = last_assistant_content
    return session


def _write_transcript(tmp_path: Path) -> str:
    """Write a minimal JSONL transcript and return its path."""
    transcript = tmp_path / "transcript.jsonl"
    lines = [
        {
            "type": "human",
            "message": {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi there!"}]},
        },
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in lines))
    return str(transcript)


def _valid_summary_prompt(body: str) -> str:
    return f"{body}\n\n## Current State\n\n## Next Steps"


def _summary_config(prompt: str = "Summary:\n{transcript_summary}") -> SessionSummaryConfig:
    return SessionSummaryConfig(
        prompt=_valid_summary_prompt(prompt),
        candidates=["claude/haiku"],
    )


def _mock_llm(summary: str) -> MagicMock:
    service = MagicMock()
    service.call_feature = AsyncMock(return_value=summary)
    return service


def _mock_candidate_llm(outputs: list[str]) -> MagicMock:
    service = MagicMock()

    async def call_feature(*_args: object, **kwargs: object) -> str:
        output_validator = kwargs.get("output_validator")
        assert callable(output_validator)
        for output in outputs:
            if output_validator(output) is None:
                return output
        raise RuntimeError("all candidates failed summary validation")

    service.call_feature = AsyncMock(side_effect=call_feature)
    return service


class _RevisionAwareSummaryManager:
    def __init__(self, session: MagicMock) -> None:
        self.session = session
        self.persist_calls: list[dict[str, object]] = []
        self.status_updates: list[tuple[str, str]] = []
        self.update_summary_calls: list[dict[str, object]] = []

    def get(self, session_id: str) -> MagicMock | None:
        return self.session if session_id == self.session.id else None

    def update_status(self, session_id: str, status: str) -> MagicMock | None:
        self.status_updates.append((session_id, status))
        self.session.status = status
        return self.session

    def update_summary(
        self,
        session_id: str,
        summary_path: str | None = None,
        summary_markdown: str | None = None,
    ) -> MagicMock | None:
        self.update_summary_calls.append(
            {
                "session_id": session_id,
                "summary_path": summary_path,
                "summary_markdown": summary_markdown,
            }
        )
        self.session.summary_markdown = summary_markdown
        return self.session

    def persist_summary_state(
        self,
        session_id: str,
        *,
        summary_markdown: str,
        generation_mode: str,
        source_context_hash: str | None = None,
        source_digest_turn_count: int | None = None,
        metadata_json: dict[str, object] | None = None,
        summary_path: str | None = None,
    ) -> MagicMock | None:
        call = {
            "session_id": session_id,
            "summary_markdown": summary_markdown,
            "generation_mode": generation_mode,
            "source_context_hash": source_context_hash,
            "source_digest_turn_count": source_digest_turn_count,
            "metadata_json": metadata_json or {},
            "summary_path": summary_path,
        }
        self.persist_calls.append(call)
        self.session.summary_markdown = summary_markdown
        self.session.summary_source_context_hash = source_context_hash
        self.session.summary_digest_turn_count = source_digest_turn_count
        self.session.summary_generation_mode = generation_mode
        return self.session


def _digest_turns(count: int) -> str:
    return "\n\n".join(f"### Turn {index}\nDigest turn {index}." for index in range(1, count + 1))


def test_digest_markdown_for_summary_strips_injected_context() -> None:
    session = _make_session(
        digest_markdown=(
            "### Turn 1\nUser: keep\nAssistant: keep\n\n"
            "<!-- gobby:injected-context:begin -->\n"
            "Injected by Gobby session handoff\n"
            "<!-- gobby:injected-context:end -->"
        ),
        last_turn_markdown=(
            "User: latest\n"
            "## Previous Session Context\n"
            "*Injected by Gobby session handoff*\n\n"
            "/Users/josh/Projects/gobby/src/gobby/memory/recall.py\n\n"
            "# Next\nAssistant: latest"
        ),
    )

    result = _digest_markdown_for_summary(session)

    assert "Injected by Gobby" not in result
    assert "/Users/josh/Projects/gobby" not in result
    assert "User: keep" in result
    assert "Assistant: latest" in result


def test_source_hash_payload_strips_injected_context_from_latest_turns() -> None:
    injected = (
        "<!-- gobby:injected-context:begin -->\n"
        "hidden runtime context\n"
        "<!-- gobby:injected-context:end -->"
    )
    session = _make_session(
        last_turn_markdown=f"Visible turn\n{injected}",
        last_assistant_content=f"{injected}\nVisible assistant",
    )

    payload = _source_hash_payload(
        session=session,
        digest_markdown="### Turn 1\nDigest.",
        summary_context={},
        prompt_template="Summary",
    )

    assert payload["last_turn_markdown"] == "Visible turn"
    assert payload["last_assistant_content"] == "Visible assistant"


@pytest.mark.asyncio
async def test_build_summary_prompt_context_strips_no_digest_fallback_inputs() -> None:
    injected = (
        "## Previous Session Context\n"
        "*Injected by Gobby session handoff*\n\n"
        "/Users/josh/Projects/gobby/src/gobby/memory/recall.py"
    )
    turns = [
        {"type": "user", "message": {"role": "user", "content": "Real prompt\n" + injected}},
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Real answer"}]},
        },
    ]
    session = _make_session(digest_markdown=None)
    handoff_ctx = MagicMock()
    handoff_ctx.git_status = ""
    manager = MagicMock()
    manager.db = None

    result = await _build_summary_prompt_context(
        session=session,
        turns=turns,
        handoff_ctx=handoff_ctx,
        db=None,
        session_manager=manager,
        project_path="/tmp",
    )

    assert "Injected by Gobby" not in result["transcript_summary"]
    assert "Injected by Gobby" not in result["last_messages"]
    assert "/Users/josh/Projects/gobby" not in result["transcript_summary"]
    assert "Real prompt" in result["transcript_summary"]


@pytest.mark.asyncio
async def test_build_summary_prompt_context_loads_unresolved_errors_off_loop() -> None:
    session = _make_session(
        session_id="sess-errors",
        digest_markdown="### Turn 1\nDigest source.",
    )
    handoff_ctx = HandoffContext()
    db = MagicMock()
    manager = MagicMock()
    manager.db = db
    records = [
        {
            "tool": "gobby-tasks/close_task",
            "target_key": "args:12345678",
            "error": "validation failed",
            "first_at": "2026-07-23T00:00:00+00:00",
            "last_at": "2026-07-23T00:00:01+00:00",
            "count": 2,
        }
    ]
    main_thread = threading.get_ident()
    loader_threads: list[int] = []

    def load_records(_db: object, session_id: str) -> list[dict[str, object]]:
        assert session_id == session.id
        loader_threads.append(threading.get_ident())
        return records

    with (
        patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
        patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
        patch("gobby.sessions.summary_context._get_claimed_tasks", return_value=""),
        patch("gobby.sessions.summary_context._get_session_memories", return_value=""),
        patch(
            "gobby.sessions.summary_context.load_open_tool_errors",
            side_effect=load_records,
        ),
    ):
        result = await _build_summary_prompt_context(
            session=session,
            turns=[],
            handoff_ctx=handoff_ctx,
            db=db,
            session_manager=manager,
            project_path="/tmp",
        )

    assert handoff_ctx.unresolved_errors == records
    assert "Unresolved Tool Errors:" in result["structured_context"]
    assert "gobby-tasks/close_task" in result["structured_context"]
    assert loader_threads and loader_threads[0] != main_thread


class TestGenerateSessionSummaries:
    """Tests for generate_session_summaries()."""

    @pytest.mark.asyncio
    async def test_no_session_manager(self) -> None:
        result = await generate_session_summaries(session_id="s1", session_manager=None)
        assert result["success"] is False
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_session_not_found(self) -> None:
        sm = MagicMock()
        sm.get.return_value = None
        result = await generate_session_summaries(session_id="s1", session_manager=sm)
        assert result["success"] is False
        assert "No session found" in result["error"]

    @pytest.mark.asyncio
    async def test_repeated_summary_persistence_keeps_postgres_connections_bounded(
        self,
        temp_db: HubDatabase,
    ) -> None:
        """Session get/update_summary/update_status calls use the bounded DB runner."""
        sm = SessionManager(temp_db)
        project = LocalProjectManager(temp_db).create(
            name="summary-project",
            repo_path="/tmp/summary-project",
        )
        session = sm.register(
            external_id="summary-bounded-db",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=project.id,
        )
        sm.update_digest_markdown(session.id, "### Turn 1\nUse digest context.")
        executor = DatabaseExecutor(max_workers=2, thread_name_prefix="summary-db")
        original_get = SessionManager.get
        first_get_started = threading.Event()
        release_gets = threading.Event()
        waits_completed: list[bool] = []

        def slow_get(self, *args, **kwargs):
            first_get_started.set()
            waits_completed.append(release_gets.wait(timeout=1))
            return original_get(self, *args, **kwargs)

        try:
            with (
                patch.object(SessionManager, "get", new=slow_get),
                patch("gobby.sessions.summarize._enrich_git_context"),
                patch(
                    "gobby.sessions.summarize._generate_full_summary",
                    return_value=(VALID_SUMMARY, None),
                ),
            ):

                async def run_summaries() -> list[dict[str, object]]:
                    return await asyncio.gather(
                        *(
                            generate_session_summaries(
                                session_id=session.id,
                                session_manager=sm,
                                db=temp_db,
                                run_db=executor.run,
                            )
                            for _ in range(20)
                        )
                    )

                task = asyncio.create_task(run_summaries())
                assert await asyncio.to_thread(first_get_started.wait, 1)
                release_gets.set()
                results = await task

            assert all(result["success"] is True for result in results)
            assert all(waits_completed)
            connection_count = getattr(temp_db, "connection_count", None)
            if connection_count is not None:
                assert connection_count <= 1 + executor.max_workers
        finally:
            executor.shutdown()
            executor.join()

    @pytest.mark.asyncio
    async def test_full_generation_persists_revision_metadata_without_existing_watermark(
        self,
    ) -> None:
        session = _make_session(
            session_id="sess-refresh",
            digest_markdown="### Turn 1\nInitial digest.",
        )
        manager = _RevisionAwareSummaryManager(session)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.sessions.summary_formatting._format_structured_context",
                return_value="structured",
            ),
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                return_value=(VALID_SUMMARY, None),
            ) as mock_full,
        ):
            result = await generate_session_summaries(
                session_id="sess-refresh",
                session_manager=manager,
                llm_service=_mock_llm(VALID_SUMMARY),
                session_summary_config=_summary_config(),
            )

        assert result["success"] is True
        assert result["generation_mode"] == "full"
        assert mock_full.call_count == 1
        assert manager.persist_calls == [
            {
                "session_id": "sess-refresh",
                "summary_markdown": VALID_SUMMARY,
                "generation_mode": "full",
                "source_context_hash": result["source_context_hash"],
                "source_digest_turn_count": 1,
                "metadata_json": {
                    "reason": "missing_summary_metadata",
                    "delta_error": None,
                    "full_error": None,
                },
                "summary_path": None,
            }
        ]

    @pytest.mark.asyncio
    async def test_concurrent_calls_share_generation_persistence_and_wiki(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        session = _make_session(
            session_id="sess-single-flight",
            digest_markdown=_digest_turns(1),
        )
        manager = _RevisionAwareSummaryManager(session)
        generation_started = asyncio.Event()
        release_generation = asyncio.Event()
        all_callers_joined = asyncio.Event()
        joined_count = 0
        original_debug = logging.getLogger("gobby.sessions.summarize").debug

        async def generate_summary(**_kwargs: object) -> tuple[str, None]:
            generation_started.set()
            await release_generation.wait()
            return VALID_SUMMARY, None

        def observe_debug(message: object, *args: object) -> None:
            nonlocal joined_count
            original_debug(message, *args)
            if message == "Joining in-flight session summary generation for %s":
                joined_count += 1
                if joined_count == 19:
                    all_callers_joined.set()

        with (
            patch("gobby.sessions.summarize._enrich_git_context", new_callable=AsyncMock),
            patch(
                "gobby.sessions.summarize._build_summary_prompt_context",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "gobby.sessions.summarize.load_summary_prompt_template",
                return_value=_valid_summary_prompt("Summary"),
            ),
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                new_callable=AsyncMock,
                side_effect=generate_summary,
            ) as mock_full,
            patch(
                "gobby.sessions.session_wiki_file.write_session_wiki_page",
                return_value={"written": True},
            ) as mock_wiki,
            patch("gobby.sessions.summarize.logger.debug", side_effect=observe_debug),
            caplog.at_level(logging.DEBUG, logger="gobby.sessions.summarize"),
        ):
            callers = [
                asyncio.create_task(
                    generate_session_summaries(
                        session_id=session.id,
                        session_manager=manager,
                    )
                )
                for _ in range(20)
            ]
            await asyncio.wait_for(generation_started.wait(), timeout=1)
            await asyncio.wait_for(all_callers_joined.wait(), timeout=1)
            release_generation.set()
            results = await asyncio.gather(*callers)

        assert mock_full.await_count == 1
        assert len(manager.persist_calls) == 1
        assert mock_wiki.call_count == 1
        assert all(result == results[0] for result in results)
        assert all(result["success"] is True for result in results)
        generated_logs = [
            record
            for record in caplog.records
            if record.getMessage().startswith("Session summary generated for")
        ]
        assert len(generated_logs) == 1
        assert generated_logs[0].levelno == logging.DEBUG
        assert joined_count == 19

    @pytest.mark.asyncio
    async def test_concurrent_callers_receive_nested_result_copies(self) -> None:
        manager = MagicMock()
        manager.db = None
        generation_started = asyncio.Event()
        joiner_attached = asyncio.Event()
        release_generation = asyncio.Event()

        async def generate_core(**_kwargs: object) -> _SummaryCoreResult:
            generation_started.set()
            await release_generation.wait()
            return _SummaryCoreResult(
                result={"success": True, "metadata": {"items": []}},
                full_markdown="summary",
            )

        def observe_debug(message: object, *_args: object) -> None:
            if str(message).startswith("Joining in-flight"):
                joiner_attached.set()

        with (
            patch(
                "gobby.sessions.summarize._generate_session_summary_core",
                side_effect=generate_core,
            ) as mock_core,
            patch(
                "gobby.sessions.summarize._write_files",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("gobby.sessions.summarize.logger.debug", side_effect=observe_debug),
        ):
            first = asyncio.create_task(generate_session_summaries("sess-deep-copy", manager))
            await asyncio.wait_for(generation_started.wait(), timeout=1)
            second = asyncio.create_task(generate_session_summaries("sess-deep-copy", manager))
            await asyncio.wait_for(joiner_attached.wait(), timeout=1)
            release_generation.set()
            first_result, second_result = await asyncio.gather(first, second)

        first_result["metadata"]["items"].append("caller-only")

        assert second_result["metadata"]["items"] == []
        assert mock_core.call_count == 1

    def test_same_session_on_different_event_loops_generates_independently(self) -> None:
        manager = MagicMock()
        manager.db = None
        barrier = threading.Barrier(2)
        generation_threads: list[int] = []
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        async def generate_core(**_kwargs: object) -> _SummaryCoreResult:
            generation_threads.append(threading.get_ident())
            barrier.wait(timeout=2)
            return _SummaryCoreResult(
                result={"success": True},
                full_markdown="summary",
            )

        async def write_files(**_kwargs: object) -> list[str]:
            return []

        def run_summary() -> None:
            try:
                results.append(asyncio.run(generate_session_summaries("sess-cross-loop", manager)))
            except BaseException as exc:
                errors.append(exc)

        with (
            patch(
                "gobby.sessions.summarize._generate_session_summary_core",
                side_effect=generate_core,
            ) as mock_core,
            patch(
                "gobby.sessions.summarize._write_files",
                side_effect=write_files,
            ),
        ):
            threads = [threading.Thread(target=run_summary) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        assert errors == []
        assert all(not thread.is_alive() for thread in threads)
        assert len(results) == 2
        assert len(set(generation_threads)) == 2
        assert mock_core.call_count == 2

    @pytest.mark.asyncio
    async def test_different_sessions_generate_in_parallel(self) -> None:
        first_session = _make_session(
            session_id="sess-parallel-1",
            digest_markdown=_digest_turns(1),
        )
        second_session = _make_session(
            session_id="sess-parallel-2",
            digest_markdown=_digest_turns(1),
        )
        first_manager = _RevisionAwareSummaryManager(first_session)
        second_manager = _RevisionAwareSummaryManager(second_session)
        entered_sessions: set[str] = set()
        both_entered = asyncio.Event()

        async def generate_summary(*, session: MagicMock, **_kwargs: object) -> tuple[str, None]:
            entered_sessions.add(session.id)
            if len(entered_sessions) == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=1)
            return VALID_SUMMARY, None

        with (
            patch("gobby.sessions.summarize._enrich_git_context", new_callable=AsyncMock),
            patch(
                "gobby.sessions.summarize._build_summary_prompt_context",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "gobby.sessions.summarize.load_summary_prompt_template",
                return_value=_valid_summary_prompt("Summary"),
            ),
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                new_callable=AsyncMock,
                side_effect=generate_summary,
            ) as mock_full,
            patch(
                "gobby.sessions.session_wiki_file.write_session_wiki_page",
                return_value={"written": True},
            ),
        ):
            results = await asyncio.gather(
                generate_session_summaries(
                    session_id=first_session.id,
                    session_manager=first_manager,
                ),
                generate_session_summaries(
                    session_id=second_session.id,
                    session_manager=second_manager,
                ),
            )

        assert entered_sessions == {"sess-parallel-1", "sess-parallel-2"}
        assert mock_full.await_count == 2
        assert all(result["success"] is True for result in results)
        assert len(first_manager.persist_calls) == 1
        assert len(second_manager.persist_calls) == 1

    @pytest.mark.asyncio
    async def test_request_after_completion_regenerates_when_digest_changes(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        session = _make_session(
            session_id="sess-later-refresh",
            digest_markdown=_digest_turns(1),
        )
        manager = _RevisionAwareSummaryManager(session)

        with (
            patch("gobby.sessions.summarize._enrich_git_context", new_callable=AsyncMock),
            patch(
                "gobby.sessions.summarize._build_summary_prompt_context",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "gobby.sessions.summarize.load_summary_prompt_template",
                return_value=_valid_summary_prompt("Summary"),
            ),
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                new_callable=AsyncMock,
                return_value=(VALID_SUMMARY, None),
            ) as mock_full,
            patch(
                "gobby.sessions.summarize._generate_delta_summary",
                new_callable=AsyncMock,
                return_value=(VALID_SUMMARY, None),
            ) as mock_delta,
            patch(
                "gobby.sessions.session_wiki_file.write_session_wiki_page",
                return_value={"written": True},
            ),
            caplog.at_level(logging.DEBUG, logger="gobby.sessions.summarize"),
        ):
            first = await generate_session_summaries(
                session_id=session.id,
                session_manager=manager,
            )
            session.digest_markdown = _digest_turns(2)
            second = await generate_session_summaries(
                session_id=session.id,
                session_manager=manager,
            )

        assert first["generation_mode"] == "full"
        assert second["generation_mode"] == "delta"
        assert mock_full.await_count == 1
        assert mock_delta.await_count == 1
        assert len(manager.persist_calls) == 2
        generated_logs = [
            record
            for record in caplog.records
            if record.getMessage().startswith("Session summary generated for")
        ]
        assert len(generated_logs) == 2
        assert all(record.levelno == logging.DEBUG for record in generated_logs)
        assert "mode=full" in generated_logs[0].getMessage()
        assert "mode=delta" in generated_logs[1].getMessage()

    @pytest.mark.asyncio
    async def test_cancelled_waiter_does_not_cancel_shared_generation(self) -> None:
        session = _make_session(
            session_id="sess-cancelled-waiter",
            digest_markdown=_digest_turns(1),
        )
        manager = _RevisionAwareSummaryManager(session)
        generation_started = asyncio.Event()
        release_generation = asyncio.Event()

        async def generate_summary(**_kwargs: object) -> tuple[str, None]:
            generation_started.set()
            await release_generation.wait()
            return VALID_SUMMARY, None

        with (
            patch("gobby.sessions.summarize._enrich_git_context", new_callable=AsyncMock),
            patch(
                "gobby.sessions.summarize._build_summary_prompt_context",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "gobby.sessions.summarize.load_summary_prompt_template",
                return_value=_valid_summary_prompt("Summary"),
            ),
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                new_callable=AsyncMock,
                side_effect=generate_summary,
            ) as mock_full,
            patch(
                "gobby.sessions.session_wiki_file.write_session_wiki_page",
                return_value={"written": True},
            ),
        ):
            cancelled_waiter = asyncio.create_task(
                generate_session_summaries(
                    session_id=session.id,
                    session_manager=manager,
                )
            )
            await asyncio.wait_for(generation_started.wait(), timeout=1)
            surviving_waiter = asyncio.create_task(
                generate_session_summaries(
                    session_id=session.id,
                    session_manager=manager,
                )
            )
            cancelled_waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled_waiter
            release_generation.set()
            result = await surviving_waiter

        assert result["success"] is True
        assert mock_full.await_count == 1
        assert len(manager.persist_calls) == 1

    @pytest.mark.asyncio
    async def test_core_failure_reaches_waiters_and_allows_retry(self) -> None:
        session = _make_session(
            session_id="sess-failure-retry",
            digest_markdown=_digest_turns(1),
        )
        manager = _RevisionAwareSummaryManager(session)
        generation_started = asyncio.Event()
        release_failure = asyncio.Event()
        second_waiter_joined = asyncio.Event()
        attempts = 0

        async def generate_summary(**_kwargs: object) -> tuple[str, None]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                generation_started.set()
                await release_failure.wait()
                raise RuntimeError("summary generation failed")
            return VALID_SUMMARY, None

        def observe_debug(message: object, *_args: object) -> None:
            if message == "Joining in-flight session summary generation for %s":
                second_waiter_joined.set()

        with (
            patch("gobby.sessions.summarize._enrich_git_context", new_callable=AsyncMock),
            patch(
                "gobby.sessions.summarize._build_summary_prompt_context",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "gobby.sessions.summarize.load_summary_prompt_template",
                return_value=_valid_summary_prompt("Summary"),
            ),
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                new_callable=AsyncMock,
                side_effect=generate_summary,
            ),
            patch(
                "gobby.sessions.session_wiki_file.write_session_wiki_page",
                return_value={"written": True},
            ) as mock_wiki,
            patch("gobby.sessions.summarize.logger.debug", side_effect=observe_debug),
        ):
            first_waiter = asyncio.create_task(
                generate_session_summaries(
                    session_id=session.id,
                    session_manager=manager,
                )
            )
            await asyncio.wait_for(generation_started.wait(), timeout=1)
            second_waiter = asyncio.create_task(
                generate_session_summaries(
                    session_id=session.id,
                    session_manager=manager,
                )
            )
            await asyncio.wait_for(second_waiter_joined.wait(), timeout=1)
            release_failure.set()
            failures = await asyncio.gather(first_waiter, second_waiter, return_exceptions=True)
            retry_result = await generate_session_summaries(
                session_id=session.id,
                session_manager=manager,
            )

        assert [str(failure) for failure in failures] == [
            "summary generation failed",
            "summary generation failed",
        ]
        assert all(isinstance(failure, RuntimeError) for failure in failures)
        assert retry_result["success"] is True
        assert attempts == 2
        assert len(manager.persist_calls) == 1
        assert mock_wiki.call_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_callers_apply_their_own_post_effects(self) -> None:
        session = _make_session(
            session_id="sess-post-effects",
            digest_markdown=_digest_turns(1),
        )
        manager = _RevisionAwareSummaryManager(session)
        generation_started = asyncio.Event()
        release_generation = asyncio.Event()
        file_callers_joined = asyncio.Event()
        joined_count = 0

        async def generate_summary(**_kwargs: object) -> tuple[str, None]:
            generation_started.set()
            await release_generation.wait()
            return VALID_SUMMARY, None

        async def write_files(
            *,
            session_id: str,
            write_file: bool,
            output_path: str,
            **_kwargs: object,
        ) -> list[str]:
            if not write_file:
                return []
            return [f"{output_path}/{session_id}-full.md"]

        def observe_debug(message: object, *_args: object) -> None:
            nonlocal joined_count
            if message == "Joining in-flight session summary generation for %s":
                joined_count += 1
                if joined_count == 2:
                    file_callers_joined.set()

        with (
            patch("gobby.sessions.summarize._enrich_git_context", new_callable=AsyncMock),
            patch(
                "gobby.sessions.summarize._build_summary_prompt_context",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "gobby.sessions.summarize.load_summary_prompt_template",
                return_value=_valid_summary_prompt("Summary"),
            ),
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                new_callable=AsyncMock,
                side_effect=generate_summary,
            ) as mock_full,
            patch(
                "gobby.sessions.summarize._write_files",
                new_callable=AsyncMock,
                side_effect=write_files,
            ) as mock_write_files,
            patch(
                "gobby.sessions.session_wiki_file.write_session_wiki_page",
                return_value={"written": True},
            ) as mock_wiki,
            patch("gobby.sessions.summarize.logger.debug", side_effect=observe_debug),
        ):
            status_only = asyncio.create_task(
                generate_session_summaries(
                    session_id=session.id,
                    session_manager=manager,
                    set_handoff_ready=True,
                    write_file=False,
                    output_path="unused",
                )
            )
            await asyncio.wait_for(generation_started.wait(), timeout=1)
            first_file = asyncio.create_task(
                generate_session_summaries(
                    session_id=session.id,
                    session_manager=manager,
                    output_path="summaries/first",
                    write_file=True,
                )
            )
            second_file = asyncio.create_task(
                generate_session_summaries(
                    session_id=session.id,
                    session_manager=manager,
                    output_path="summaries/second",
                    write_file=True,
                )
            )
            await asyncio.wait_for(file_callers_joined.wait(), timeout=1)
            release_generation.set()
            results = await asyncio.gather(status_only, first_file, second_file)

        assert mock_full.await_count == 1
        assert len(manager.persist_calls) == 1
        assert mock_wiki.call_count == 1
        assert manager.status_updates == [(session.id, "handoff_ready")]
        assert results[0]["files_written"] == []
        assert results[1]["files_written"] == ["summaries/first/sess-post-effects-full.md"]
        assert results[2]["files_written"] == ["summaries/second/sess-post-effects-full.md"]
        assert len({id(result) for result in results}) == 3
        assert mock_write_files.await_count == 3

    @pytest.mark.asyncio
    async def test_all_invalid_candidate_reasons_persist_with_digest_fallback(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        session = _make_session(
            session_id="sess-invalid-candidates",
            digest_markdown="### Turn 1\nInitial digest.",
        )
        manager = _RevisionAwareSummaryManager(session)
        candidate_error = (
            "No text generation candidate succeeded "
            "(tried: ['qwen/qwen-model', 'claude/haiku']; "
            "errors: [('qwen/qwen-model', 'missing Current State'), "
            "('claude/haiku', 'missing Next Steps')])"
        )

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.sessions.summary_formatting._format_structured_context",
                return_value="structured",
            ),
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                return_value=(None, candidate_error),
            ),
            patch(
                "gobby.sessions.summarize._format_deterministic_summary",
                return_value=VALID_SUMMARY,
            ),
            caplog.at_level(logging.WARNING, logger="gobby.sessions.summarize"),
        ):
            result = await generate_session_summaries(
                session_id=session.id,
                session_manager=manager,
                llm_service=_mock_llm(VALID_SUMMARY),
                session_summary_config=_summary_config(),
                set_handoff_ready=False,
            )

        assert result["success"] is True
        assert result["generation_mode"] == "digest_fallback"
        metadata = manager.persist_calls[0]["metadata_json"]
        assert isinstance(metadata, dict)
        assert metadata["full_error"] == candidate_error
        failure_record = next(
            record
            for record in caplog.records
            if record.getMessage().startswith("Full LLM summary failed for")
        )
        assert failure_record.levelno == logging.WARNING

    @pytest.mark.asyncio
    async def test_source_hash_match_returns_existing_summary_without_regeneration(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        session = _make_session(
            session_id="sess-noop",
            digest_markdown="### Turn 1\nStable digest.",
        )
        manager = _RevisionAwareSummaryManager(session)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.sessions.summary_formatting._format_structured_context",
                return_value="structured",
            ),
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                return_value=(VALID_SUMMARY, None),
            ) as mock_full,
            patch(
                "gobby.sessions.session_wiki_file.write_session_wiki_page",
                return_value={"written": True},
            ),
            caplog.at_level(logging.DEBUG, logger="gobby.sessions.summarize"),
        ):
            first = await generate_session_summaries(
                session_id="sess-noop",
                session_manager=manager,
                llm_service=_mock_llm(VALID_SUMMARY),
                session_summary_config=_summary_config(),
            )
            second = await generate_session_summaries(
                session_id="sess-noop",
                session_manager=manager,
                llm_service=_mock_llm(VALID_SUMMARY),
                session_summary_config=_summary_config(),
            )

        assert first["generation_mode"] == "full"
        assert second["generation_mode"] == "noop"
        assert second["refresh_reason"] == "source_context_hash_match"
        assert mock_full.call_count == 1
        assert len(manager.persist_calls) == 1
        summary_records = [
            record
            for record in caplog.records
            if record.name == "gobby.sessions.summarize"
            and record.getMessage().startswith("Session summary ")
        ]
        assert [record.levelno for record in summary_records] == [
            logging.DEBUG,
            logging.DEBUG,
        ]
        assert "mode=full" in summary_records[0].getMessage()
        assert "reason=missing_summary_metadata" in summary_records[0].getMessage()
        assert "output_chars=" in summary_records[0].getMessage()
        assert "mode=noop" in summary_records[1].getMessage()
        assert "reason=source_context_hash_match" in summary_records[1].getMessage()

    @pytest.mark.asyncio
    async def test_delta_merge_receives_only_digest_turns_since_watermark(self) -> None:
        session = _make_session(
            session_id="sess-delta",
            summary_markdown=VALID_SUMMARY,
            digest_markdown=_digest_turns(3),
        )
        session.summary_source_context_hash = "old-hash"
        session.summary_digest_turn_count = 1
        manager = _RevisionAwareSummaryManager(session)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.sessions.summary_formatting._format_structured_context",
                return_value="structured",
            ),
            patch(
                "gobby.sessions.summarize._generate_delta_summary",
                new=AsyncMock(return_value=(VALID_SUMMARY, None)),
            ) as mock_delta,
            patch("gobby.sessions.summarize._generate_full_summary") as mock_full,
        ):
            result = await generate_session_summaries(
                session_id="sess-delta",
                session_manager=manager,
                llm_service=_mock_llm(VALID_SUMMARY),
                session_summary_config=_summary_config(),
            )

        assert result["generation_mode"] == "delta"
        assert manager.persist_calls[-1]["generation_mode"] == "delta"
        assert mock_full.call_count == 0
        new_digest_turns = mock_delta.await_args.kwargs["new_digest_turns"]
        assert "### Turn 1" not in new_digest_turns
        assert "### Turn 2" in new_digest_turns
        assert "### Turn 3" in new_digest_turns

    @pytest.mark.asyncio
    async def test_digest_delta_threshold_uses_full_rebuild(self) -> None:
        session = _make_session(
            session_id="sess-threshold",
            summary_markdown=VALID_SUMMARY,
            digest_markdown=_digest_turns(21),
        )
        session.summary_source_context_hash = "old-hash"
        session.summary_digest_turn_count = 1
        manager = _RevisionAwareSummaryManager(session)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.sessions.summary_formatting._format_structured_context",
                return_value="structured",
            ),
            patch("gobby.sessions.summarize._generate_delta_summary") as mock_delta,
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                return_value=(VALID_SUMMARY, None),
            ) as mock_full,
        ):
            result = await generate_session_summaries(
                session_id="sess-threshold",
                session_manager=manager,
                llm_service=_mock_llm(VALID_SUMMARY),
                session_summary_config=_summary_config(),
            )

        assert result["generation_mode"] == "full"
        assert result["refresh_reason"] == "digest_delta_threshold_reached"
        assert mock_delta.call_count == 0
        assert mock_full.call_count == 1

    @pytest.mark.asyncio
    async def test_invalid_delta_output_falls_back_to_full_generation(self) -> None:
        session = _make_session(
            session_id="sess-delta-invalid",
            summary_markdown=VALID_SUMMARY,
            digest_markdown=_digest_turns(2),
        )
        session.summary_source_context_hash = "old-hash"
        session.summary_digest_turn_count = 1
        manager = _RevisionAwareSummaryManager(session)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.sessions.summary_formatting._format_structured_context",
                return_value="structured",
            ),
            patch(
                "gobby.sessions.summarize._generate_delta_summary",
                new=AsyncMock(return_value=(None, "delta failed")),
            ),
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                return_value=(VALID_SUMMARY, None),
            ) as mock_full,
        ):
            result = await generate_session_summaries(
                session_id="sess-delta-invalid",
                session_manager=manager,
                llm_service=_mock_llm(VALID_SUMMARY),
                session_summary_config=_summary_config(),
            )

        assert result["generation_mode"] == "full"
        assert result["delta_error"] == "delta failed"
        assert mock_full.call_count == 1
        assert manager.persist_calls[-1]["summary_markdown"] == VALID_SUMMARY

    @pytest.mark.asyncio
    async def test_no_transcript_path(self) -> None:
        sm = MagicMock()
        sm.get.return_value = _make_session(transcript_path=None)
        result = await generate_session_summaries(session_id="s1", session_manager=sm)
        assert result["success"] is False
        assert "No transcript path" in result["error"]

    @pytest.mark.asyncio
    async def test_transcript_not_found(self) -> None:
        sm = MagicMock()
        sm.get.return_value = _make_session(transcript_path="/nonexistent/path.jsonl")
        result = await generate_session_summaries(session_id="s1", session_manager=sm)
        assert result["success"] is False
        assert "Transcript file not found" in result["error"]

    @pytest.mark.asyncio
    async def test_compact_only(self, tmp_path: Path) -> None:
        transcript_path = _write_transcript(tmp_path)
        sm = MagicMock()
        sm.get.return_value = _make_session(transcript_path=transcript_path)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch(
                "gobby.sessions.formatting.format_handoff_as_markdown",
                return_value="# Compact Summary\nHello world.",
            ),
        ):
            result = await generate_session_summaries(
                session_id="sess-1",
                session_manager=sm,
                compact_only=True,
            )

        assert result["success"] is True
        # compact_only is ignored — always generates full summary via fallback
        assert result["full_length"] > 0

    @pytest.mark.asyncio
    async def test_sets_handoff_ready(self, tmp_path: Path) -> None:
        transcript_path = _write_transcript(tmp_path)
        sm = MagicMock()
        sm.get.return_value = _make_session(transcript_path=transcript_path)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch(
                "gobby.sessions.formatting.format_handoff_as_markdown",
                return_value="### Recent Activity\n- Completed the requested implementation work.",
            ),
        ):
            result = await generate_session_summaries(
                session_id="sess-1",
                session_manager=sm,
                compact_only=True,
                set_handoff_ready=True,
            )

        assert result["success"] is True
        sm.update_status.assert_called_once_with("sess-1", "handoff_ready")

    @pytest.mark.asyncio
    async def test_skips_handoff_ready_when_disabled(self, tmp_path: Path) -> None:
        transcript_path = _write_transcript(tmp_path)
        sm = MagicMock()
        sm.get.return_value = _make_session(transcript_path=transcript_path)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch(
                "gobby.sessions.formatting.format_handoff_as_markdown",
                return_value="### Recent Activity\n- Completed the requested implementation work.",
            ),
        ):
            result = await generate_session_summaries(
                session_id="sess-1",
                session_manager=sm,
                compact_only=True,
                set_handoff_ready=False,
            )

        assert result["success"] is True
        sm.update_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_does_not_set_handoff_ready(self, tmp_path: Path) -> None:
        # Synchronous lifecycle handlers own handoff_ready transitions, so
        # callers must opt in explicitly; the default must never flip status.
        transcript_path = _write_transcript(tmp_path)
        sm = MagicMock()
        sm.get.return_value = _make_session(transcript_path=transcript_path)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch(
                "gobby.sessions.formatting.format_handoff_as_markdown",
                return_value="### Recent Activity\n- Completed the requested implementation work.",
            ),
        ):
            result = await generate_session_summaries(
                session_id="sess-1",
                session_manager=sm,
                compact_only=True,
            )

        assert result["success"] is True
        sm.update_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_summary_with_llm(self, tmp_path: Path) -> None:
        transcript_path = _write_transcript(tmp_path)
        sm = MagicMock()
        sm.get.return_value = _make_session(transcript_path=transcript_path)

        mock_llm = _mock_llm(VALID_SUMMARY)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch("gobby.sessions.formatting.format_handoff_as_markdown", return_value="# Compact"),
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                return_value=(VALID_SUMMARY, None),
            ),
        ):
            result = await generate_session_summaries(
                session_id="sess-1",
                session_manager=sm,
                llm_service=mock_llm,
                session_summary_config=_summary_config(),
            )

        assert result["success"] is True
        assert result["full_length"] > 0

    @pytest.mark.asyncio
    async def test_full_only_error_returns_failure(self, tmp_path: Path) -> None:
        transcript_path = _write_transcript(tmp_path)
        sm = MagicMock()
        sm.get.return_value = _make_session(transcript_path=transcript_path)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch(
                "gobby.sessions.summarize._generate_full_summary", return_value=(None, "LLM error")
            ),
            patch(
                "gobby.sessions.formatting.format_handoff_as_markdown",
                return_value="### Recent Activity\n- Preserved deterministic fallback context.",
            ),
        ):
            result = await generate_session_summaries(
                session_id="sess-1",
                session_manager=sm,
                full_only=True,
            )

        # full_only flag is ignored — fallback to code-only renderer on LLM error
        assert result["success"] is True
        assert result["full_length"] > 0

    @pytest.mark.asyncio
    async def test_full_summary_uses_droid_parser_with_transcript_path(self) -> None:
        session = _make_session(
            session_id="sess-droid",
            transcript_path="/tmp/droid-session.jsonl",
            source="droid",
        )
        handoff_ctx = MagicMock()
        handoff_ctx.git_status = ""
        session_manager = MagicMock()
        session_manager.db = None
        llm_service = _mock_llm(VALID_SUMMARY)

        with (
            patch("gobby.prompts.loader.PromptLoader") as MockPromptLoader,
            patch("gobby.sessions.transcripts.droid.DroidTranscriptParser") as MockParser,
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.sessions.summary_formatting._format_structured_context",
                return_value="structured",
            ),
            patch("gobby.sessions.summary_formatting.format_turns_for_llm", return_value="turns"),
        ):
            MockPromptLoader.return_value.load.return_value.content = _valid_summary_prompt(
                "Droid:\n{transcript_summary}"
            )
            MockParser.return_value.extract_turns_since_clear.return_value = [{"type": "message"}]
            MockParser.return_value.extract_last_messages.return_value = [
                {"role": "user", "content": "hi"}
            ]

            full_markdown, full_error = await _generate_full_summary(
                session=session,
                turns=[{"type": "message"}],
                handoff_ctx=handoff_ctx,
                llm_service=llm_service,
                session_summary_config=_summary_config("Droid:\n{transcript_summary}"),
                db=None,
                session_manager=session_manager,
            )

        assert full_markdown == VALID_SUMMARY
        assert full_error is None
        MockParser.assert_called_once_with(
            session_id="sess-droid",
            transcript_path="/tmp/droid-session.jsonl",
        )

    @pytest.mark.asyncio
    async def test_full_summary_uses_qwen_parser_for_qwen_source(self) -> None:
        session = _make_session(
            session_id="sess-qwen",
            transcript_path="/tmp/qwen-session.json",
            source="qwen",
        )
        handoff_ctx = MagicMock()
        handoff_ctx.git_status = ""
        session_manager = MagicMock()
        session_manager.db = None
        llm_service = _mock_llm(VALID_SUMMARY)

        with (
            patch("gobby.prompts.loader.PromptLoader") as MockPromptLoader,
            patch("gobby.sessions.transcripts.qwen.QwenTranscriptParser") as MockQwenParser,
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.sessions.summary_formatting._format_structured_context",
                return_value="structured",
            ),
            patch("gobby.sessions.summary_formatting.format_turns_for_llm", return_value="turns"),
        ):
            MockPromptLoader.return_value.load.return_value.content = _valid_summary_prompt(
                "Qwen:\n{transcript_summary}"
            )
            MockQwenParser.return_value.extract_turns_since_clear.return_value = [
                {"type": "message"}
            ]
            MockQwenParser.return_value.extract_last_messages.return_value = [
                {"role": "user", "content": "hi"}
            ]

            full_markdown, full_error = await _generate_full_summary(
                session=session,
                turns=[{"type": "message"}],
                handoff_ctx=handoff_ctx,
                llm_service=llm_service,
                session_summary_config=_summary_config("Qwen:\n{transcript_summary}"),
                db=None,
                session_manager=session_manager,
            )

        assert full_markdown == VALID_SUMMARY
        assert full_error is None
        MockQwenParser.assert_called_once_with(session_id="sess-qwen")

    @pytest.mark.asyncio
    async def test_digest_primary_context_does_not_format_full_transcript(self) -> None:
        session = _make_session(
            session_id="sess-digest",
            transcript_path="/tmp/transcript.jsonl",
            digest_markdown="### Turn 1\nDigest is the bounded source.",
        )
        handoff_ctx = MagicMock()
        handoff_ctx.git_status = "clean"
        session_manager = MagicMock()
        session_manager.db = None
        llm_service = _mock_llm(VALID_SUMMARY)

        with (
            patch("gobby.prompts.loader.PromptLoader") as MockPromptLoader,
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.sessions.summary_formatting._format_structured_context",
                return_value="structured",
            ),
            patch("gobby.sessions.summary_formatting.format_turns_for_llm") as mock_format,
        ):
            MockPromptLoader.return_value.load.return_value.content = _valid_summary_prompt(
                "Transcript:\n{transcript_summary}\nLast:\n{last_messages}"
            )

            full_markdown, full_error = await _generate_full_summary(
                session=session,
                turns=[{"message": {"role": "user", "content": "raw transcript"}}],
                handoff_ctx=handoff_ctx,
                llm_service=llm_service,
                session_summary_config=_summary_config(
                    "Transcript:\n{transcript_summary}\nLast:\n{last_messages}"
                ),
                db=None,
                session_manager=session_manager,
            )

        assert full_markdown == VALID_SUMMARY
        assert full_error is None
        mock_format.assert_not_called()
        prompt = llm_service.call_feature.await_args.args[1]
        assert "Transcript:\n### Turn 1\nDigest is the bounded source." in prompt
        assert "Last:\n### Turn 1\nDigest is the bounded source." in prompt

    @pytest.mark.asyncio
    async def test_digest_primary_context_includes_latest_turn_when_digest_lags(self) -> None:
        session = _make_session(
            session_id="sess-digest",
            transcript_path="/tmp/transcript.jsonl",
            digest_markdown="### Turn 1\nOld coordinator state.",
            last_turn_markdown="Current build state: #12746 is development:in_progress.",
        )
        handoff_ctx = MagicMock()
        handoff_ctx.git_status = "clean"
        session_manager = MagicMock()
        session_manager.db = None
        llm_service = _mock_llm(VALID_SUMMARY)

        with (
            patch("gobby.prompts.loader.PromptLoader") as MockPromptLoader,
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.sessions.summary_formatting._format_structured_context",
                return_value="structured",
            ),
            patch("gobby.sessions.summary_formatting.format_turns_for_llm") as mock_format,
        ):
            MockPromptLoader.return_value.load.return_value.content = _valid_summary_prompt(
                "Transcript:\n{transcript_summary}\nLast:\n{last_messages}"
            )

            full_markdown, full_error = await _generate_full_summary(
                session=session,
                turns=[{"message": {"role": "user", "content": "raw transcript"}}],
                handoff_ctx=handoff_ctx,
                llm_service=llm_service,
                session_summary_config=_summary_config(
                    "Transcript:\n{transcript_summary}\nLast:\n{last_messages}"
                ),
                db=None,
                session_manager=session_manager,
            )

        assert full_markdown == VALID_SUMMARY
        assert full_error is None
        mock_format.assert_not_called()
        prompt = llm_service.call_feature.await_args.args[1]
        assert "Old coordinator state." in prompt
        assert "Current build state: #12746 is development:in_progress." in prompt
        prompt_body = prompt.split("\n\n## Current State", maxsplit=1)[0]
        assert prompt_body.rstrip().endswith(
            "Current build state: #12746 is development:in_progress."
        )

    @pytest.mark.asyncio
    async def test_digest_primary_context_includes_current_assistant_content(self) -> None:
        session = _make_session(
            session_id="sess-digest",
            transcript_path="/tmp/transcript.jsonl",
            digest_markdown="### Turn 1\nOld coordinator state.",
            last_turn_markdown="Old coordinator state.",
            last_assistant_content="Current handoff: #14997 open and #12746 still running.",
        )
        handoff_ctx = MagicMock()
        handoff_ctx.git_status = "clean"
        session_manager = MagicMock()
        session_manager.db = None
        llm_service = _mock_llm(VALID_SUMMARY)

        with (
            patch("gobby.prompts.loader.PromptLoader") as MockPromptLoader,
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.sessions.summary_formatting._format_structured_context",
                return_value="structured",
            ),
            patch("gobby.sessions.summary_formatting.format_turns_for_llm") as mock_format,
        ):
            MockPromptLoader.return_value.load.return_value.content = _valid_summary_prompt(
                "Transcript:\n{transcript_summary}\nLast:\n{last_messages}"
            )

            full_markdown, full_error = await _generate_full_summary(
                session=session,
                turns=[{"message": {"role": "user", "content": "raw transcript"}}],
                handoff_ctx=handoff_ctx,
                llm_service=llm_service,
                session_summary_config=_summary_config(
                    "Transcript:\n{transcript_summary}\nLast:\n{last_messages}"
                ),
                db=None,
                session_manager=session_manager,
            )

        assert full_markdown == VALID_SUMMARY
        assert full_error is None
        mock_format.assert_not_called()
        prompt = llm_service.call_feature.await_args.args[1]
        assert "Old coordinator state." in prompt
        assert "Current handoff: #14997 open and #12746 still running." in prompt
        prompt_body = prompt.split("\n\n## Current State", maxsplit=1)[0]
        assert prompt_body.rstrip().endswith(
            "Current handoff: #14997 open and #12746 still running."
        )

    @pytest.mark.asyncio
    async def test_full_summary_enrichment_uses_run_db(self) -> None:
        session = _make_session(
            session_id="sess-enrich",
            transcript_path="/tmp/transcript.jsonl",
            digest_markdown="### Turn 1\nDigest source.",
        )
        handoff_ctx = MagicMock()
        handoff_ctx.git_status = ""
        session_manager = MagicMock()
        session_manager.db = MagicMock()
        llm_service = _mock_llm(VALID_SUMMARY)
        run_db_calls = []

        async def run_db(func, *args, **kwargs):
            run_db_calls.append(func)
            return func(*args, **kwargs)

        with (
            patch("gobby.prompts.loader.PromptLoader") as MockPromptLoader,
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.sessions.summary_formatting._format_structured_context",
                return_value="structured",
            ),
            patch(
                "gobby.sessions.summary_context._get_claimed_tasks",
                return_value="task context",
            ) as claimed,
            patch(
                "gobby.sessions.summary_context._get_session_memories",
                return_value="memory context",
            ) as memories,
        ):
            MockPromptLoader.return_value.load.return_value.content = _valid_summary_prompt(
                "Tasks:\n{claimed_tasks}\nMemories:\n{session_memories}"
            )

            full_markdown, full_error = await _generate_full_summary(
                session=session,
                turns=[],
                handoff_ctx=handoff_ctx,
                llm_service=llm_service,
                session_summary_config=_summary_config(
                    "Tasks:\n{claimed_tasks}\nMemories:\n{session_memories}"
                ),
                db=session_manager.db,
                session_manager=session_manager,
                run_db=run_db,
            )

        assert full_markdown == VALID_SUMMARY
        assert full_error is None
        assert claimed in run_db_calls
        assert memories in run_db_calls
        prompt = llm_service.call_feature.await_args.args[1]
        assert "Tasks:\ntask context" in prompt
        assert "Memories:\nmemory context" in prompt

    @pytest.mark.asyncio
    async def test_summary_git_context_uses_session_terminal_cwd(self) -> None:
        session = _make_session(
            session_id="sess-cwd",
            digest_markdown="### Turn 1\nDigest source.",
        )
        session.terminal_context = {"cwd": "/workspace/project"}
        manager = _RevisionAwareSummaryManager(session)

        async def record_session_edit(handoff_ctx: HandoffContext, _cwd: Path) -> None:
            handoff_ctx.files_modified.append("src/gobby/sessions/summary_context.py")

        with (
            patch(
                "gobby.sessions.summarize._enrich_git_context",
                new=AsyncMock(side_effect=record_session_edit),
            ) as enrich,
            patch(
                "gobby.workflows.git_utils.get_file_changes", return_value="file changes"
            ) as files,
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value="diff") as diff,
            patch(
                "gobby.sessions.summary_formatting._format_structured_context",
                return_value="structured",
            ),
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                return_value=(VALID_SUMMARY, None),
            ),
        ):
            result = await generate_session_summaries(
                session_id="sess-cwd",
                session_manager=manager,
                llm_service=_mock_llm(VALID_SUMMARY),
                session_summary_config=_summary_config(),
            )

        assert result["success"] is True
        enrich.assert_awaited_once()
        assert enrich.await_args.args[1] == Path("/workspace/project")
        files.assert_called_once_with(
            project_path="/workspace/project",
            paths=("src/gobby/sessions/summary_context.py",),
        )
        diff.assert_called_once_with(
            project_path="/workspace/project",
            paths=("src/gobby/sessions/summary_context.py",),
        )

    @pytest.mark.asyncio
    async def test_missing_digest_uses_bounded_transcript_fallback(self) -> None:
        session = _make_session(session_id="sess-fallback", transcript_path="/tmp/transcript.jsonl")
        handoff_ctx = MagicMock()
        handoff_ctx.git_status = ""
        session_manager = MagicMock()
        session_manager.db = None
        llm_service = _mock_llm(VALID_SUMMARY)
        turns = [{"idx": i} for i in range(TRANSCRIPT_FALLBACK_MAX_TURNS + 20)]
        formatted = "fallback\n" + ("x" * (TRANSCRIPT_FALLBACK_MAX_CHARS + 100))

        with (
            patch("gobby.prompts.loader.PromptLoader") as MockPromptLoader,
            patch("gobby.sessions.transcripts.claude.ClaudeTranscriptParser") as MockParser,
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.sessions.summary_formatting._format_structured_context",
                return_value="structured",
            ),
            patch(
                "gobby.sessions.summary_formatting.format_turns_for_llm",
                return_value=formatted,
            ) as mock_format,
        ):
            MockPromptLoader.return_value.load.return_value.content = _valid_summary_prompt(
                "Transcript:\n{transcript_summary}"
            )
            MockParser.return_value.extract_turns_since_clear.return_value = turns
            MockParser.return_value.extract_last_messages.return_value = []

            full_markdown, full_error = await _generate_full_summary(
                session=session,
                turns=turns,
                handoff_ctx=handoff_ctx,
                llm_service=llm_service,
                session_summary_config=_summary_config("Transcript:\n{transcript_summary}"),
                db=None,
                session_manager=session_manager,
            )

        assert full_markdown == VALID_SUMMARY
        assert full_error is None
        formatted_turns = mock_format.call_args.args[0]
        assert len(formatted_turns) == TRANSCRIPT_FALLBACK_MAX_TURNS
        prompt = llm_service.call_feature.await_args.args[1]
        transcript_summary = prompt.removeprefix("Transcript:\n").split(
            "\n\n## Current State",
            maxsplit=1,
        )[0]
        assert len(transcript_summary) <= TRANSCRIPT_FALLBACK_MAX_CHARS
        assert transcript_summary.endswith("... [truncated]")

    @pytest.mark.asyncio
    async def test_full_summary_uses_secondary_candidate_after_validation_rejection(
        self,
    ) -> None:
        session = _make_session(
            session_id="sess-secondary",
            digest_markdown="### Turn 1\nDigest source.",
        )
        session_manager = MagicMock()
        llm_service = _mock_candidate_llm(
            [
                "Detailed prose without the mandatory semantic sections. " * 4,
                VALID_SUMMARY,
            ]
        )

        full_markdown, full_error = await _generate_full_summary(
            session=session,
            turns=[],
            handoff_ctx=MagicMock(),
            llm_service=llm_service,
            session_summary_config=_summary_config(),
            db=None,
            session_manager=session_manager,
            summary_context={},
            prompt_template=_valid_summary_prompt("Summarize the session."),
        )

        assert full_markdown == VALID_SUMMARY
        assert full_error is None

    @pytest.mark.asyncio
    async def test_delta_summary_uses_secondary_candidate_after_validation_rejection(
        self,
    ) -> None:
        session = _make_session(session_id="sess-delta-secondary")
        session_manager = MagicMock()
        llm_service = _mock_candidate_llm(
            [
                "Detailed prose without the mandatory semantic sections. " * 4,
                VALID_SUMMARY,
            ]
        )

        with patch("gobby.prompts.loader.PromptLoader") as prompt_loader:
            prompt_loader.return_value.load.return_value.content = _valid_summary_prompt(
                "Previous: {previous_summary}\nNew: {new_digest_turns}"
            )
            merged_markdown, merge_error = await _generate_delta_summary(
                session=session,
                previous_summary=VALID_SUMMARY,
                new_digest_turns="### Turn 2\nNew state.",
                summary_context={},
                llm_service=llm_service,
                session_summary_config=_summary_config(),
                db=MagicMock(spec=HubDatabase),
                session_manager=session_manager,
            )

        assert merged_markdown == VALID_SUMMARY
        assert merge_error is None

    @pytest.mark.asyncio
    async def test_invalid_custom_full_prompt_skips_llm_call(self) -> None:
        session = _make_session(session_id="sess-invalid-prompt")
        session_manager = MagicMock()
        llm_service = _mock_llm(VALID_SUMMARY)

        full_markdown, full_error = await _generate_full_summary(
            session=session,
            turns=[],
            handoff_ctx=MagicMock(),
            llm_service=llm_service,
            session_summary_config=SessionSummaryConfig(
                prompt="Summarize without the required headings.",
                candidates=["claude/haiku"],
            ),
            db=None,
            session_manager=session_manager,
            summary_context={},
            prompt_template="Summarize without the required headings.",
        )

        assert full_markdown is None
        assert full_error == (
            "Invalid summary prompt template: summary prompt must include literal "
            "required heading(s): ## Current State, ## Next Steps"
        )
        llm_service.call_feature.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_provider_summary_returns_generic_error(self) -> None:
        session = _make_session(
            session_id="sess-invalid",
            transcript_path="/tmp/transcript.jsonl",
            digest_markdown="### Turn 1\nDigest source.",
        )
        handoff_ctx = MagicMock()
        handoff_ctx.git_status = ""
        session_manager = MagicMock()
        session_manager.db = None
        llm_service = _mock_llm("Session summary generation failed: provider down")

        with (
            patch("gobby.prompts.loader.PromptLoader") as MockPromptLoader,
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.sessions.summary_formatting._format_structured_context",
                return_value="structured",
            ),
        ):
            MockPromptLoader.return_value.load.return_value.content = _valid_summary_prompt(
                "Summary:\n{transcript_summary}"
            )

            full_markdown, full_error = await _generate_full_summary(
                session=session,
                turns=[],
                handoff_ctx=handoff_ctx,
                llm_service=llm_service,
                session_summary_config=_summary_config(),
                db=None,
                session_manager=session_manager,
            )

        assert full_markdown is None
        assert full_error == (
            "Generated session summary was invalid: summary begins with a provider failure sentinel"
        )

    @pytest.mark.asyncio
    async def test_shutdown_cancellation_propagates_as_task_cancellation(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from gobby.llm.claude_runtime import ClaudeSDKShutdownCancellation

        session = _make_session(session_id="sess-shutdown")
        session_manager = MagicMock()
        llm_service = MagicMock()
        llm_service.call_feature = AsyncMock(
            side_effect=ClaudeSDKShutdownCancellation("summary cancelled")
        )

        with (
            caplog.at_level(logging.INFO, logger="gobby.sessions.summary_generation"),
            pytest.raises(asyncio.CancelledError),
        ):
            await _generate_full_summary(
                session=session,
                turns=[],
                handoff_ctx=MagicMock(),
                llm_service=llm_service,
                session_summary_config=_summary_config(),
                db=None,
                session_manager=session_manager,
                summary_context={},
                prompt_template=_valid_summary_prompt("Summary"),
            )

        llm_service.call_feature.assert_awaited_once()
        assert "cancelled during daemon shutdown" in caplog.text
        assert not any(record.levelno >= logging.WARNING for record in caplog.records)

    @pytest.mark.asyncio
    async def test_provider_failure_string_is_not_persisted(self, tmp_path: Path) -> None:
        transcript_path = _write_transcript(tmp_path)
        sm = MagicMock()
        sm.get.return_value = _make_session(transcript_path=transcript_path)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                return_value=("Session summary generation failed: provider unavailable", None),
            ),
            patch(
                "gobby.sessions.formatting.format_handoff_as_markdown",
                return_value="### Recent Activity\n- Preserved deterministic fallback context.",
            ),
        ):
            result = await generate_session_summaries(
                session_id="sess-1",
                session_manager=sm,
                set_handoff_ready=False,
            )

        assert result["success"] is True
        sm.update_summary.assert_called_once_with(
            "sess-1",
            summary_markdown=(
                "## Current State\n\n"
                "### Recent Activity\n- Preserved deterministic fallback context.\n\n"
                "## Next Steps\n\nContinue from the captured session state."
            ),
        )
        assert sm.update_summary.call_count == 1
        assert sm.update_summary.call_args is not None

    @pytest.mark.asyncio
    async def test_total_generation_failure_does_not_set_handoff_ready(self) -> None:
        session = _make_session(
            session_id="sess-total-failure",
            digest_markdown="### Turn 1\nDigest source.",
        )
        sm = MagicMock()
        sm.get.return_value = session

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch("gobby.sessions.summarize._generate_full_summary", return_value=(None, "down")),
            patch("gobby.sessions.summarize._format_deterministic_summary", return_value=""),
        ):
            result = await generate_session_summaries(
                session_id=session.id,
                session_manager=sm,
                set_handoff_ready=True,
            )

        assert result["success"] is False
        assert result["full_length"] == 0
        assert result["error"] == "Unable to generate a valid session summary"
        sm.update_summary.assert_not_called()
        sm.update_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_deterministic_fallback_persists_latest_turn_when_digest_lags(self) -> None:
        sm = MagicMock()
        sm.get.return_value = _make_session(
            digest_markdown="### Turn 1\nOld compact handoff.",
            last_turn_markdown="Fresh compact handoff: #14653 is needs_review.",
        )

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                return_value=(None, "provider unavailable"),
            ),
            patch(
                "gobby.sessions.formatting.format_handoff_as_markdown",
                return_value="### Recent Activity\n- Preserved deterministic fallback context.",
            ),
        ):
            result = await generate_session_summaries(
                session_id="sess-1",
                session_manager=sm,
                set_handoff_ready=False,
            )

        assert result["success"] is True
        persisted = sm.update_summary.call_args.kwargs["summary_markdown"]
        assert "Old compact handoff." in persisted
        assert "Fresh compact handoff: #14653 is needs_review." in persisted


class TestGetClaimedTasks:
    """Tests for _get_claimed_tasks()."""

    def _task_state_defaults(self, task: MagicMock, state: str) -> None:
        task.closed_at = None
        task.escalated_at = None
        task.is_escalated = False
        task.current_stage = {"state": state}

    def test_returns_empty_on_no_tasks(self) -> None:
        """Returns empty string when session has no tasks."""
        from gobby.sessions.summarize import _get_claimed_tasks

        mock_db = MagicMock()
        with patch("gobby.storage.session_tasks.SessionTaskManager") as MockSTM:
            MockSTM.return_value.get_session_tasks.return_value = []
            result = _get_claimed_tasks("sess-1", mock_db)
        assert result == ""

    def test_formats_task_with_seq_num(self) -> None:
        """Formats tasks with seq_num refs and descriptions."""
        from gobby.sessions.summarize import _get_claimed_tasks

        mock_task = MagicMock()
        mock_task.id = "task-uuid-1234"
        mock_task.seq_num = 42
        self._task_state_defaults(mock_task, "in_progress")
        mock_task.title = "Fix the bug"
        mock_task.description = "A short description"

        mock_db = MagicMock()
        with (
            patch("gobby.storage.session_tasks.SessionTaskManager") as MockSTM,
            patch("gobby.storage.task_dependencies.TaskDependencyManager") as MockDep,
        ):
            MockSTM.return_value.get_session_tasks.return_value = [{"task": mock_task}]
            MockDep.return_value.get_all_dependencies.return_value = []
            result = _get_claimed_tasks("sess-1", mock_db)

        assert "#42" in result
        assert "[in_progress]" in result
        assert "Fix the bug" in result
        assert "A short description" in result

    def test_formats_task_without_seq_num(self) -> None:
        """Tasks without seq_num use truncated ID as ref."""
        from gobby.sessions.summarize import _get_claimed_tasks

        mock_task = MagicMock()
        mock_task.id = "task-uuid-1234-full"
        mock_task.seq_num = None
        self._task_state_defaults(mock_task, "ready")
        mock_task.title = "No seq num task"
        mock_task.description = None

        mock_db = MagicMock()
        with (
            patch("gobby.storage.session_tasks.SessionTaskManager") as MockSTM,
            patch("gobby.storage.task_dependencies.TaskDependencyManager") as MockDep,
        ):
            MockSTM.return_value.get_session_tasks.return_value = [{"task": mock_task}]
            MockDep.return_value.get_all_dependencies.return_value = []
            result = _get_claimed_tasks("sess-1", mock_db)

        assert "task-uui" in result
        assert "[ready]" in result

    def test_formats_task_with_blockers(self) -> None:
        """Tasks with blocking dependencies show blocker info."""
        from gobby.sessions.summarize import _get_claimed_tasks

        mock_task = MagicMock()
        mock_task.id = "task-uuid-1234"
        mock_task.seq_num = 5
        self._task_state_defaults(mock_task, "ready")
        mock_task.title = "Blocked task"
        mock_task.description = None

        mock_dep = MagicMock()
        mock_dep.dep_type = "blocks"
        mock_dep.depends_on = "blocker-id-xyz"

        mock_db = MagicMock()
        with (
            patch("gobby.storage.session_tasks.SessionTaskManager") as MockSTM,
            patch("gobby.storage.task_dependencies.TaskDependencyManager") as MockDep,
        ):
            MockSTM.return_value.get_session_tasks.return_value = [{"task": mock_task}]
            MockDep.return_value.get_all_dependencies.return_value = [mock_dep]
            result = _get_claimed_tasks("sess-1", mock_db)

        assert "Blocked by:" in result
        assert "blocker-" in result

    def test_long_description_truncated(self) -> None:
        """Descriptions longer than 120 chars are truncated."""
        from gobby.sessions.summarize import _get_claimed_tasks

        mock_task = MagicMock()
        mock_task.id = "task-uuid-1234"
        mock_task.seq_num = 1
        self._task_state_defaults(mock_task, "ready")
        mock_task.title = "Long desc task"
        mock_task.description = "A" * 200

        mock_db = MagicMock()
        with (
            patch("gobby.storage.session_tasks.SessionTaskManager") as MockSTM,
            patch("gobby.storage.task_dependencies.TaskDependencyManager") as MockDep,
        ):
            MockSTM.return_value.get_session_tasks.return_value = [{"task": mock_task}]
            MockDep.return_value.get_all_dependencies.return_value = []
            result = _get_claimed_tasks("sess-1", mock_db)

        assert "..." in result
        assert "Long desc task" in result

    def test_exception_returns_empty(self) -> None:
        """Exception during task lookup returns empty string."""
        from gobby.sessions.summarize import _get_claimed_tasks

        mock_db = MagicMock()
        with patch(
            "gobby.storage.session_tasks.SessionTaskManager", side_effect=RuntimeError("fail")
        ):
            result = _get_claimed_tasks("sess-1", mock_db)
        assert result == ""


class TestGetSessionMemories:
    """Tests for _get_session_memories()."""

    def test_returns_empty_on_no_memories(self) -> None:
        from gobby.sessions.summarize import _get_session_memories

        mock_db = MagicMock()
        mock_db.fetchall.return_value = []
        result = _get_session_memories("sess-1", mock_db)
        assert result == ""

    def test_formats_memories(self) -> None:
        from gobby.sessions.summarize import _get_session_memories

        mock_db = MagicMock()
        mock_db.fetchall.return_value = [
            {"content": "Remember this fact", "tags": '["tag1", "tag2"]', "memory_type": "fact"},
        ]
        result = _get_session_memories("sess-1", mock_db)
        assert "[fact]" in result
        assert "Remember this fact" in result
        assert "tag1, tag2" in result

    def test_truncates_long_content(self) -> None:
        from gobby.sessions.summarize import _get_session_memories

        mock_db = MagicMock()
        mock_db.fetchall.return_value = [
            {"content": "X" * 300, "tags": None, "memory_type": None},
        ]
        result = _get_session_memories("sess-1", mock_db)
        assert "..." in result
        assert "[fact]" in result  # default memory_type

    def test_invalid_tags_json_kept_as_string(self) -> None:
        from gobby.sessions.summarize import _get_session_memories

        mock_db = MagicMock()
        mock_db.fetchall.return_value = [
            {"content": "data", "tags": "not-json", "memory_type": "note"},
        ]
        result = _get_session_memories("sess-1", mock_db)
        assert "not-json" in result

    def test_exception_returns_empty(self) -> None:
        from gobby.sessions.summarize import _get_session_memories

        mock_db = MagicMock()
        mock_db.fetchall.side_effect = RuntimeError("db error")
        result = _get_session_memories("sess-1", mock_db)
        assert result == ""


class TestExtractDigestTurns:
    """Tests for _extract_digest_turns()."""

    def test_none_input(self) -> None:
        from gobby.sessions.summarize import _extract_digest_turns

        first, recent = _extract_digest_turns(None)
        assert first == ""
        assert recent == ""

    def test_empty_string(self) -> None:
        from gobby.sessions.summarize import _extract_digest_turns

        first, recent = _extract_digest_turns("")
        assert first == ""
        assert recent == ""

    def test_no_turn_structure(self) -> None:
        from gobby.sessions.summarize import _extract_digest_turns

        text = "Just some text without turn headers. " * 20
        first, recent = _extract_digest_turns(text)
        assert len(first) <= 500
        assert recent == ""

    def test_single_turn(self) -> None:
        from gobby.sessions.summarize import _extract_digest_turns

        text = "### Turn 1\nDid some work."
        first, recent = _extract_digest_turns(text)
        assert "Turn 1" in first
        assert "Did some work" in first

    def test_multiple_turns(self) -> None:
        from gobby.sessions.summarize import _extract_digest_turns

        text = (
            "### Turn 1\nFirst turn content.\n"
            "### Turn 2\nSecond turn content.\n"
            "### Turn 3\nThird turn content.\n"
        )
        first, recent = _extract_digest_turns(text)
        assert "Turn 1" in first
        assert "Turn 2" in recent or "Turn 3" in recent

    def test_truncation_on_long_turns(self) -> None:
        from gobby.sessions.summarize import _extract_digest_turns

        long_content = "X" * 2000
        text = f"### Turn 1\n{long_content}\n### Turn 2\n{long_content}\n"
        first, recent = _extract_digest_turns(text)
        assert len(first) <= 810  # 800 + up to 10 chars for "..." suffix
        assert len(recent) <= 1510


class TestReadTranscript:
    """Tests for _read_transcript()."""

    @pytest.mark.asyncio
    async def test_reads_valid_jsonl(self, tmp_path: Path) -> None:
        from gobby.sessions.summarize import _read_transcript

        path = tmp_path / "transcript.jsonl"
        lines = [
            json.dumps({"type": "human", "content": "hello"}),
            json.dumps({"type": "assistant", "content": "hi"}),
        ]
        path.write_text("\n".join(lines))
        turns = await _read_transcript(path)
        assert len(turns) == 2

    @pytest.mark.asyncio
    async def test_streams_only_requested_jsonl_tail(self, tmp_path: Path) -> None:
        from gobby.sessions.summarize import _read_transcript

        path = tmp_path / "transcript.jsonl"
        path.write_text("\n".join(json.dumps({"index": index}) for index in range(200)))

        turns = await _read_transcript(path, max_turns=10)

        assert [turn["index"] for turn in turns] == list(range(190, 200))

    @pytest.mark.asyncio
    async def test_rejects_malformed_lines(self, tmp_path: Path) -> None:
        from gobby.sessions.summarize import _read_transcript
        from gobby.sessions.transcripts.base import TranscriptReadError

        path = tmp_path / "transcript.jsonl"
        lines = [
            json.dumps({"type": "human"}),
            "not valid json{{{",
            json.dumps({"type": "assistant"}),
        ]
        path.write_text("\n".join(lines))
        with pytest.raises(TranscriptReadError) as error:
            await _read_transcript(path)

        assert error.value.byte_offset == len(lines[0]) + 1
        assert error.value.line_number is None

    @pytest.mark.asyncio
    async def test_skips_empty_lines(self, tmp_path: Path) -> None:
        from gobby.sessions.summarize import _read_transcript

        path = tmp_path / "transcript.jsonl"
        path.write_text(json.dumps({"type": "human"}) + "\n\n\n")
        turns = await _read_transcript(path)
        assert len(turns) == 1

    @pytest.mark.asyncio
    async def test_rejects_non_dict_json_values(self, tmp_path: Path) -> None:
        """Non-dict JSON values are durable corrupt transcript records."""
        from gobby.sessions.summarize import _read_transcript
        from gobby.sessions.transcripts.base import TranscriptReadError

        path = tmp_path / "transcript.jsonl"
        lines = [
            json.dumps({"type": "assistant"}),
            json.dumps("bare string"),
            json.dumps(42),
            json.dumps({"type": "user"}),
        ]
        path.write_text("\n".join(lines))
        with pytest.raises(TranscriptReadError) as error:
            await _read_transcript(path)

        assert error.value.byte_offset == len(lines[0]) + 1
        assert error.value.line_number is None

    @pytest.mark.asyncio
    async def test_json_extension_uses_line_oriented_reader(self, tmp_path: Path) -> None:
        """A .json file still uses the source's line-oriented transcript format."""
        from gobby.sessions.summarize import _read_transcript

        path = tmp_path / "transcript.json"
        lines = [
            json.dumps({"type": "user", "message": {"role": "user", "parts": [{"text": "hello"}]}}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"role": "model", "parts": [{"text": "hi"}]},
                }
            ),
        ]
        path.write_text("\n".join(lines))
        turns = await _read_transcript(path, source="qwen")
        assert len(turns) == 2


class TestWriteFiles:
    """Tests for _write_files()."""

    @pytest.mark.asyncio
    async def test_no_write_when_disabled(self) -> None:
        from gobby.sessions.summarize import _write_files

        sm = MagicMock()
        result = await _write_files(
            session_id="s1",
            full_markdown="# Full",
            write_file=False,
            output_path="~/.gobby/summaries",
            session_manager=sm,
        )
        assert result == []
