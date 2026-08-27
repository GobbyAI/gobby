"""Focused acceptance tests for pre-summary digest dispatch."""

from __future__ import annotations

import asyncio
import gc
import logging
import threading
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks.hook_manager import HookManager
from gobby.hooks.session_summary_dispatcher import SessionSummaryDispatcher


def _dispatcher(
    *,
    loop: asyncio.AbstractEventLoop | None,
    memory_manager: Any | None = None,
    session_manager: Any | None = None,
    llm_service: Any | None = None,
    config: Any | None = None,
) -> SessionSummaryDispatcher:
    return SessionSummaryDispatcher(
        session_manager=session_manager or MagicMock(),
        llm_service=llm_service or MagicMock(),
        session_summary_config=MagicMock(),
        database=MagicMock(),
        loop=loop,
        logger=logging.getLogger("tests.session-summary-dispatcher"),
        memory_manager=memory_manager,
        config=config,
    )


async def _dispatch_and_wait(
    dispatcher: SessionSummaryDispatcher,
    *,
    session_id: str = "session-1",
) -> None:
    done = threading.Event()
    dispatcher.dispatch(session_id, done_event=done)
    assert await asyncio.to_thread(done.wait, 2)


@pytest.mark.asyncio
async def test_dispatch_digests_before_summary() -> None:
    events: list[str] = []

    async def digest(**_kwargs: Any) -> dict[str, int]:
        events.append("digest")
        return {"turn_num": 3}

    async def summarize(**_kwargs: Any) -> dict[str, bool]:
        events.append("summary")
        return {"success": True}

    loop = asyncio.get_running_loop()
    with (
        patch("gobby.memory.digest.build_turn_and_digest", side_effect=digest) as mock_digest,
        patch("gobby.sessions.summarize.generate_session_summaries", side_effect=summarize),
    ):
        await _dispatch_and_wait(_dispatcher(loop=loop, memory_manager=MagicMock()))
        assert events == ["digest", "summary"]

        events.clear()
        await _dispatch_and_wait(_dispatcher(loop=loop, memory_manager=None))

    assert events == ["summary"]
    mock_digest.assert_awaited_once()


@pytest.mark.parametrize(
    ("outcome", "summary_calls", "log_text"),
    [
        pytest.param({"error": "boom"}, 1, "pre-summary digest failed", id="error"),
        pytest.param(
            {"cancelled": True, "reason": "shutdown"},
            1,
            "pre-summary digest failed",
            id="cancelled",
        ),
        pytest.param(None, 1, None, id="nothing-undigested"),
        pytest.param(
            {"error": "corrupt", "error_kind": "transcript_read"},
            0,
            "transcript corruption",
            id="transcript-corruption",
        ),
    ],
)
@pytest.mark.asyncio
async def test_dispatch_treats_returned_digest_errors_as_failures(
    outcome: dict[str, Any] | None,
    summary_calls: int,
    log_text: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop = asyncio.get_running_loop()
    summarize = AsyncMock(return_value={"success": True})
    with (
        patch("gobby.memory.digest.build_turn_and_digest", new=AsyncMock(return_value=outcome)),
        patch("gobby.sessions.summarize.generate_session_summaries", summarize),
        caplog.at_level(logging.DEBUG, logger="tests.session-summary-dispatcher"),
    ):
        await _dispatch_and_wait(_dispatcher(loop=loop, memory_manager=MagicMock()))

    assert summarize.await_count == summary_calls
    if log_text is not None:
        assert log_text in caplog.text


@pytest.mark.asyncio
async def test_pre_digest_follows_daemon_loop_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    digest = AsyncMock(return_value={"turn_num": 1})
    summarize = AsyncMock(return_value={"success": True})
    with (
        patch("gobby.memory.digest.build_turn_and_digest", digest),
        patch("gobby.sessions.summarize.generate_session_summaries", summarize),
        caplog.at_level(logging.DEBUG, logger="tests.session-summary-dispatcher"),
    ):
        await _dispatch_and_wait(_dispatcher(loop=None, memory_manager=MagicMock()))

        def dispatch_without_running_loop() -> bool:
            done = threading.Event()
            _dispatcher(loop=None, memory_manager=MagicMock()).dispatch(
                "session-thread",
                done_event=done,
            )
            return done.wait(2)

        assert await asyncio.to_thread(dispatch_without_running_loop)

        daemon_loop = asyncio.new_event_loop()
        started = threading.Event()

        def run_daemon_loop() -> None:
            asyncio.set_event_loop(daemon_loop)
            daemon_loop.call_soon(started.set)
            daemon_loop.run_forever()

        daemon_thread = threading.Thread(target=run_daemon_loop)
        daemon_thread.start()
        assert started.wait(2)

        def dispatch_to_daemon_without_running_loop() -> bool:
            done = threading.Event()
            _dispatcher(loop=daemon_loop, memory_manager=MagicMock()).dispatch(
                "session-daemon",
                done_event=done,
            )
            return done.wait(2)

        try:
            assert await asyncio.to_thread(dispatch_to_daemon_without_running_loop)
        finally:
            daemon_loop.call_soon_threadsafe(daemon_loop.stop)
            await asyncio.to_thread(daemon_thread.join, 2)
            daemon_loop.close()

    assert digest.await_count == 1
    assert summarize.await_count == 3
    assert caplog.text.count("pre-summary digest skipped: no daemon loop") == 2


def _hook_components(*, memory_manager: Any) -> MagicMock:
    components = MagicMock()
    components.memory_manager = memory_manager
    components.config = SimpleNamespace(session_summary="summary-config")
    components.session_manager = MagicMock()
    return components


def test_hook_manager_wires_memory_manager_into_dispatcher() -> None:
    memory_manager = object()
    components = _hook_components(memory_manager=memory_manager)
    dispatcher = MagicMock()
    with (
        patch("gobby.hooks.hook_manager.HookManagerFactory.create", return_value=components),
        patch(
            "gobby.hooks.hook_manager.build_session_summary_dispatcher",
            return_value=dispatcher,
        ) as build_dispatcher,
    ):
        manager = HookManager(llm_service=MagicMock())
        manager._dispatch_session_summaries("session-1")

    assert manager._memory_manager is memory_manager
    assert build_dispatcher.call_args.kwargs["memory_manager"] is memory_manager
    assert build_dispatcher.call_args.kwargs["config"] is components.config
    dispatcher.dispatch.assert_called_once_with(
        "session-1",
        _background=False,
        done_event=None,
        set_handoff_ready=False,
    )


@pytest.mark.parametrize(
    ("corrupt_tail", "expected_log"),
    [
        pytest.param(b'{"broken":}\n', "transcript corruption", id="malformed-json"),
        pytest.param(b"\xff\n", "transcript corruption", id="invalid-utf8"),
        pytest.param(b'{"split":"\xe2\x82', "in-flight tail", id="partial-multibyte"),
    ],
)
@pytest.mark.asyncio
async def test_transcript_corruption_never_persists_a_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt_tail: bytes,
    expected_log: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(b'{"message":{"role":"user","content":"compact now"}}\n' + corrupt_tail)
    monkeypatch.setattr("gobby.memory.digest.TRANSCRIPT_TAIL_RETRY_DELAY_SECONDS", 0)
    session = SimpleNamespace(
        transcript_path=str(transcript),
        source="claude",
        last_digested_pair_index=0,
        last_digest_input_hash=None,
        summary_markdown="previous summary",
    )
    session_manager = MagicMock()
    session_manager.get.return_value = session
    memory_manager = MagicMock()
    memory_manager.config.enabled = True
    config = SimpleNamespace(digest=SimpleNamespace(enabled=True, num_pairs=50))
    summarize = AsyncMock(return_value={"success": True})

    with (
        patch("gobby.sessions.summarize.generate_session_summaries", summarize),
        caplog.at_level(logging.INFO, logger="tests.session-summary-dispatcher"),
    ):
        await _dispatch_and_wait(
            _dispatcher(
                loop=asyncio.get_running_loop(),
                memory_manager=memory_manager,
                session_manager=session_manager,
                llm_service=AsyncMock(),
                config=config,
            )
        )

    assert session.summary_markdown == "previous summary"
    assert expected_log in caplog.text
    summarize.assert_not_awaited()
    session_manager.persist_summary_state.assert_not_called()
    session_manager.update_summary.assert_not_called()


@pytest.mark.asyncio
async def test_tail_withheld_defers_summary_until_pair_digested() -> None:
    session = SimpleNamespace(
        summary_markdown="prior summary",
        summary_source_context_hash="prior-hash",
        summary_digest_turn_count=1,
    )
    session_manager = MagicMock()
    session_manager.get.return_value = session

    async def summarize(**_kwargs: Any) -> dict[str, bool]:
        session.summary_markdown = "summary with compact-triggering tool facts"
        return {"success": True}

    digest = AsyncMock(
        side_effect=[
            {
                "tail_withheld": True,
                "withheld_pair": {"prompt": "compact now", "activity": "tool pending"},
            },
            {"turn_num": 2},
        ]
    )
    with (
        patch("gobby.memory.digest.build_turn_and_digest", digest),
        patch("gobby.sessions.summarize.generate_session_summaries", side_effect=summarize) as gen,
    ):
        dispatcher = _dispatcher(
            loop=asyncio.get_running_loop(),
            memory_manager=MagicMock(),
            session_manager=session_manager,
        )
        await _dispatch_and_wait(dispatcher)
        assert session.summary_markdown == "prior summary"
        assert session.summary_source_context_hash == "prior-hash"
        assert session.summary_digest_turn_count == 1
        gen.assert_not_awaited()

        await _dispatch_and_wait(dispatcher)

    assert session.summary_markdown == "summary with compact-triggering tool facts"
    gen.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_from_foreign_loop_digests_on_daemon_loop() -> None:
    daemon_loop = asyncio.new_event_loop()
    started = threading.Event()

    def run_daemon_loop() -> None:
        asyncio.set_event_loop(daemon_loop)
        daemon_loop.call_soon(started.set)
        daemon_loop.run_forever()

    thread = threading.Thread(target=run_daemon_loop)
    thread.start()
    assert started.wait(2)
    digest_loops: list[asyncio.AbstractEventLoop] = []
    digest_lock = asyncio.Lock()

    async def digest(**_kwargs: Any) -> dict[str, int]:
        async with digest_lock:
            digest_loops.append(asyncio.get_running_loop())
        return {"turn_num": 1}

    try:
        with (
            patch("gobby.memory.digest.build_turn_and_digest", side_effect=digest),
            patch(
                "gobby.sessions.summarize.generate_session_summaries",
                new=AsyncMock(return_value={"success": True}),
            ),
        ):
            await _dispatch_and_wait(_dispatcher(loop=daemon_loop, memory_manager=MagicMock()))
    finally:
        daemon_loop.call_soon_threadsafe(daemon_loop.stop)
        await asyncio.to_thread(thread.join, 2)
        daemon_loop.close()

    assert digest_loops == [daemon_loop]
    assert not thread.is_alive()


@pytest.mark.asyncio
async def test_rejected_daemon_submission_closes_coroutine_and_releases_waiter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    daemon_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    daemon_loop.is_running.return_value = True
    done = threading.Event()
    digest = AsyncMock(return_value={"turn_num": 1})
    summarize = AsyncMock(return_value={"success": True})

    with (
        warnings.catch_warnings(record=True) as caught,
        patch(
            "gobby.hooks.session_summary_dispatcher.asyncio.run_coroutine_threadsafe",
            side_effect=RuntimeError("loop closed"),
        ),
        patch("gobby.memory.digest.build_turn_and_digest", digest),
        patch("gobby.sessions.summarize.generate_session_summaries", summarize),
        caplog.at_level(logging.WARNING, logger="tests.session-summary-dispatcher"),
    ):
        warnings.simplefilter("always")
        _dispatcher(loop=daemon_loop, memory_manager=MagicMock()).dispatch(
            "session-1",
            done_event=done,
        )
        gc.collect()

    assert done.is_set()
    assert "failed to schedule: loop closed" in caplog.text
    assert not [warning for warning in caught if issubclass(warning.category, RuntimeWarning)]
    digest.assert_not_awaited()
    summarize.assert_not_awaited()
