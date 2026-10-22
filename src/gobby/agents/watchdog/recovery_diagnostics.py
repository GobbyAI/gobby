"""Read recovery evidence and record watchdog diagnostics without choosing recovery policy."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import psycopg

from gobby.tasks.state_semantics import projected_task_state
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.agents.watchdog import WatchdogReaderRegistry
    from gobby.agents.watchdog.models import WatchdogTranscriptSnapshot
    from gobby.agents.watchdog.transcript_resolver import WatchdogTranscriptResolver
    from gobby.storage.agents import AgentRun
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager

# Keep recovery diagnostics on the coordinator's existing log channel.
logger = logging.getLogger("gobby.agents.watchdog.recovery")
WATCHDOG_ACTOR = "agent_idle_watchdog"


class WatchdogRecoveryDiagnostics:
    """Diagnostic operations over dependencies owned by the recovery coordinator."""

    db: HubDatabase
    _run_db: Callable[..., Awaitable[Any]]
    _task_manager: LocalTaskManager | None
    _get_session_manager: Callable[[], SessionManager | None]
    _watchdog_readers: WatchdogReaderRegistry
    _transcript_resolver: WatchdogTranscriptResolver

    async def _session_made_successful_mcp_call(self, run: AgentRun) -> bool | None:
        """Read observed MCP availability, returning None on unavailable evidence."""
        session_id = run.child_session_id
        if not session_id:
            return None
        try:
            variables = await self._run_db(
                SessionVariableManager(self.db).get_variables,
                session_id,
            )
        except psycopg.DatabaseError:
            logger.warning(
                "Database error loading session variables for MCP-availability check on "
                "run %s session %s",
                run.id,
                session_id,
                exc_info=True,
            )
            return None
        if not isinstance(variables, dict):
            return None
        return bool(variables.get("mcp_calls"))

    async def _record_watchdog_task_event(
        self,
        run: AgentRun,
        *,
        action: str,
        session_id: str | None,
        detail: str,
    ) -> None:
        """Append a durable task-history event for watchdog recovery actions."""
        if self._task_manager is None or not run.task_id:
            return

        try:
            task = await self._run_db(self._task_manager.get_task, run.task_id)
        except (psycopg.Error, ValueError):
            logger.warning(
                "Failed to load task %s for idle watchdog audit on run %s",
                run.task_id,
                run.id,
                exc_info=True,
            )
            return

        try:
            state: str = projected_task_state(task)
        except Exception:
            state = "agent_watchdog"

        reason = f"agent_idle_watchdog:{action} run_id={run.id}"
        if session_id:
            reason = f"{reason} session_id={session_id}"
        reason = f"{reason} {detail}"

        try:
            await self._run_db(
                self._task_manager.lifecycle_events.record_lifecycle_event,
                run.task_id,
                from_state=state,
                to_state=state,
                reason=reason,
                by_actor=WATCHDOG_ACTOR,
            )
        except (psycopg.Error, ValueError):
            logger.warning(
                "Failed to record idle watchdog audit for run %s task %s",
                run.id,
                run.task_id,
                exc_info=True,
            )

    async def _log_transcript_snapshot(
        self,
        run: AgentRun,
        *,
        reason: str,
        snapshot: WatchdogTranscriptSnapshot | None = None,
        level: int = logging.WARNING,
    ) -> None:
        session_id = run.child_session_id
        if not session_id:
            return

        if snapshot is None:
            session_manager = self._get_session_manager()
            if session_manager is None:
                return

            try:
                session = await self._run_db(session_manager.get, session_id)
            except Exception:
                logger.warning(
                    "Failed to load session %s for watchdog idle diagnostics on run %s",
                    session_id,
                    run.id,
                )
                return

            if session is None:
                return

            provider_id = session.source or run.provider
            reader = self._watchdog_readers.for_provider(provider_id)
            if reader is None:
                return
            transcript_path = await self._transcript_resolver.resolve(session, run_id=run.id)
            if transcript_path is None:
                logger.warning(
                    "Watchdog idle diagnostic for %s run %s (%s): "
                    "session %s has no readable transcript path",
                    reader.provider_id,
                    run.id,
                    reason,
                    session_id,
                )
                return

            try:
                snapshot = await reader.read(transcript_path)
            except OSError:
                logger.warning(
                    "Failed to read %s transcript for idle diagnostic on run %s (%s)",
                    reader.provider_id,
                    run.id,
                    reason,
                )
                return

        if not snapshot.tail and snapshot.latest_turn_event is None:
            logger.warning(
                "Watchdog idle diagnostic for %s run %s (%s): "
                "no transcript summaries for session %s",
                snapshot.provider,
                run.id,
                reason,
                session_id,
            )
            return

        log_args = (
            snapshot.provider,
            run.id,
            reason,
            session_id,
            json.dumps(snapshot.to_log_dict(), ensure_ascii=True),
        )
        if level == logging.WARNING:
            logger.warning(
                "Watchdog idle diagnostic for %s run %s (%s) session %s: %s",
                *log_args,
            )
        else:
            logger.log(
                level,
                "Watchdog idle diagnostic for %s run %s (%s) session %s: %s",
                *log_args,
            )
