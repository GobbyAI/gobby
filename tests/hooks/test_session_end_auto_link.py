"""Regression coverage for lifecycle-managed session-end commit auto-linking."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.session_end_auto_link import SessionEndAutoLinkWorker
from gobby.storage.session_models import Session
from gobby.tasks.commits import AutoLinkResult


def _session() -> Session:
    timestamp = datetime(2026, 8, 2, tzinfo=UTC)
    return Session(
        id="session-19495",
        external_id="external-19495",
        machine_id="21000000-0000-4000-8000-000000000011",
        source="codex",
        project_id="project-19495",
        title="Session auto-link regression",
        status="active",
        transcript_path=None,
        summary_path=None,
        summary_markdown=None,
        git_branch=None,
        parent_session_id=None,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _event() -> HookEvent:
    return HookEvent(
        event_type=HookEventType.SESSION_END,
        session_id="external-19495",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"cwd": "/tmp/session-19495"},
        machine_id="21000000-0000-4000-8000-000000000011",
        metadata={"_platform_session_id": "session-19495"},
    )


def _handlers(
    worker: SessionEndAutoLinkWorker,
) -> tuple[EventHandlers, MagicMock]:
    session_manager = MagicMock()
    session_manager.get.return_value = _session()
    handlers = EventHandlers(
        session_manager=session_manager,
        task_manager=MagicMock(),
        session_end_auto_link_worker=worker,
    )
    return handlers, session_manager


def test_session_end_returns_while_slow_auto_link_is_managed_until_shutdown() -> None:
    link_started = threading.Event()
    release_link = threading.Event()
    link_finished = threading.Event()
    handler_finished = threading.Event()
    shutdown_finished = threading.Event()
    auto_link_calls: list[dict[str, object]] = []
    handler_responses: list[HookResponse] = []
    thread_failures: list[BaseException] = []

    def slow_auto_link(**kwargs: object) -> AutoLinkResult:
        auto_link_calls.append(kwargs)
        link_started.set()
        release_link.wait()
        link_finished.set()
        return AutoLinkResult(linked_tasks={}, total_linked=0, skipped=0)

    worker = SessionEndAutoLinkWorker(
        database=MagicMock(),
        task_manager=MagicMock(),
        logger=logging.getLogger("gobby.hooks.test"),
    )
    handlers, session_manager = _handlers(worker)

    def run_handler() -> None:
        try:
            handler_responses.append(handlers.handle_session_end(_event()))
        except BaseException as exc:
            thread_failures.append(exc)
        finally:
            handler_finished.set()

    def shutdown_worker() -> None:
        try:
            worker.shutdown()
        except BaseException as exc:
            thread_failures.append(exc)
        finally:
            shutdown_finished.set()

    with (
        patch("gobby.hooks.session_end_auto_link.LocalProjectManager") as project_manager_cls,
        patch(
            "gobby.hooks.session_end_auto_link.auto_link_commits",
            side_effect=slow_auto_link,
        ),
    ):
        project_manager_cls.return_value.get.return_value = SimpleNamespace(name="gobby")
        handler_thread = threading.Thread(target=run_handler)
        handler_thread.start()
        returned_inside_budget = handler_finished.wait(0.5)
        started_in_background = link_started.wait(0.5)
        status_updated_before_auto_link_finished = (
            session_manager.update_status_if_non_terminal.call_count == 1
            and not link_finished.is_set()
        )

        shutdown_thread = threading.Thread(target=shutdown_worker)
        shutdown_thread.start()
        shutdown_waited_for_job = not shutdown_finished.wait(0.1)

        release_link.set()
        handler_thread.join(timeout=1)
        shutdown_thread.join(timeout=1)

    assert returned_inside_budget
    assert started_in_background
    assert status_updated_before_auto_link_finished
    assert shutdown_waited_for_job
    assert shutdown_finished.is_set()
    assert link_finished.is_set()
    assert thread_failures == []
    assert handler_responses[0].decision == "allow"
    call_kwargs = auto_link_calls[0]
    assert call_kwargs["since"] == "2026-08-02T00:00:00+00:00"
    assert call_kwargs["cwd"] == "/tmp/session-19495"
    session_manager.update_status_if_non_terminal.assert_called_once_with(
        "session-19495", "expired"
    )


def test_session_end_auto_link_failure_is_logged_and_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = SessionEndAutoLinkWorker(
        database=MagicMock(),
        task_manager=MagicMock(),
        logger=logging.getLogger("gobby.hooks.test"),
    )
    handlers, _ = _handlers(worker)

    with (
        caplog.at_level(logging.ERROR, logger="gobby.hooks.test"),
        patch("gobby.hooks.session_end_auto_link.LocalProjectManager") as project_manager_cls,
        patch(
            "gobby.hooks.session_end_auto_link.auto_link_commits",
            side_effect=RuntimeError("deliberate auto-link failure"),
        ),
        patch("gobby.hooks.session_end_auto_link.inc_counter") as inc_counter,
        patch("gobby.hooks.session_end_auto_link.inc_gauge"),
        patch("gobby.hooks.session_end_auto_link.dec_gauge"),
    ):
        project_manager_cls.return_value.get.return_value = SimpleNamespace(name="gobby")
        response = handlers.handle_session_end(_event())
        worker.shutdown()

    assert response.decision == "allow"
    assert (
        "commit auto-link failed for session session-19495 project project-19495: "
        "deliberate auto-link failure"
    ) in caplog.text
    inc_counter.assert_any_call(
        "background_tasks_failed_total",
        attributes={"component": "session_end_commit_auto_link"},
    )
