from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.agents.idle_detector import IdleDetector
from gobby.utils.datetime import parse_stored_datetime

if TYPE_CHECKING:
    from gobby.agents.agent_cleanup import AgentCleanupHandler
    from gobby.agents.tmux.session_manager import TmuxSessionManager
    from gobby.config.tmux import TmuxConfig
    from gobby.storage.agents import AgentRun, LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


class IdleCheckHandler:
    """Handles idle detection and reprompting for agents."""

    def __init__(
        self,
        agent_run_manager: LocalAgentRunManager,
        get_session_manager: Callable[[], SessionManager | None],
        tmux: TmuxSessionManager,
        idle_detector: IdleDetector,
        cleanup_handler: AgentCleanupHandler,
        tmux_config: TmuxConfig,
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._agent_run_manager = agent_run_manager
        self._get_session_manager = get_session_manager
        self._tmux = tmux
        self._idle_detector = idle_detector
        self._cleanup_handler = cleanup_handler
        self._tmux_config = tmux_config
        self._run_db = run_db

    async def _run_sqlite(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db(func, *args, **kwargs)

    async def check_idle_agents(self) -> int:
        """Check for idle agents and reprompt or fail them."""
        if not self._tmux_config.idle_check_enabled:
            return 0

        runs = await self._run_sqlite(self._get_active_terminal_runs)

        handled = 0
        for run in runs:
            try:
                handled += await self._handle_idle_check(run)
            except Exception as e:
                logger.warning(f"Error checking idle state for agent {run.id}: {e}")

        return handled

    def _get_active_terminal_runs(self) -> list[AgentRun]:
        """Get active terminal agent runs with tmux sessions from DB."""
        runs = self._agent_run_manager.list_active()
        return [r for r in runs if r.tmux_session_name]

    def _idle_timeout_seconds_for_run(self, run: AgentRun) -> int:
        """Return the idle timeout window for a run."""
        requested_effort = (run.requested_reasoning_effort or "").strip().lower()
        if requested_effort == "xhigh":
            return self._tmux_config.idle_timeout_seconds * 5
        return self._tmux_config.idle_timeout_seconds

    async def _handle_idle_check(self, run: AgentRun) -> int:
        """Handle idle check for a single agent."""
        latest_run = await self._run_sqlite(self._agent_run_manager.get, run.id)
        if latest_run is None or latest_run.status not in ("pending", "running"):
            self._idle_detector.reset_idle(run.id)
            return 0

        run = latest_run
        tmux_name = run.tmux_session_name
        if tmux_name is None:
            logger.warning("Skipping idle check for run %s: missing tmux name", run.id)
            self._idle_detector.reset_idle(run.id)
            return 0
        idle_timeout_seconds = self._idle_timeout_seconds_for_run(run)

        session_stale = False
        session_id = run.child_session_id or run.parent_session_id
        session_manager = self._get_session_manager()

        if session_id and session_manager:
            session = await self._run_sqlite(session_manager.get, session_id)
            if session and session.updated_at:
                try:
                    last_update = parse_stored_datetime(session.updated_at)
                    if last_update is not None:
                        elapsed = (datetime.now(UTC) - last_update).total_seconds()
                        if elapsed < idle_timeout_seconds:
                            self._idle_detector.reset_idle(run.id)
                            return 0
                        else:
                            session_stale = True
                except (ValueError, TypeError):
                    pass

        pane_output = await self._tmux.capture_pane(tmux_name, lines=15)
        if pane_output is None and not session_stale:
            return 0

        if pane_output is not None:
            status = self._idle_detector.detect(pane_output)

            if status == "context_full":
                logger.info(f"Agent {run.id} hit context window limit - failing")
                await self._fail_idle_agent(run, reason="context window exhausted")
                return 1

            if not session_stale and status == "active":
                self._idle_detector.reset_idle(run.id)
                return 0

        if self._idle_detector.should_fail(run.id, self._tmux_config.max_reprompt_attempts):
            logger.info(
                f"Agent {run.id} still idle after "
                f"{self._tmux_config.max_reprompt_attempts} reprompts — failing"
            )
            await self._log_recent_codex_response_items(
                run,
                reason="failing after max idle reprompts",
            )
            await self._fail_idle_agent(run, reason="idle after max reprompt attempts")
            return 1

        if self._idle_detector.should_reprompt(
            run.id,
            idle_timeout_seconds,
            self._tmux_config.max_reprompt_attempts,
        ):
            logger.info(f"Reprompting idle agent {run.id}")
            await self._log_recent_codex_response_items(
                run,
                reason="reprompting apparently idle agent",
            )
            sent = await self._tmux.send_keys(tmux_name, IdleDetector.REPROMPT_MESSAGE + "\n")
            if sent:
                self._idle_detector.record_reprompt(run.id)
            return 1

        return 0

    async def _fail_idle_agent(self, run: AgentRun, reason: str) -> None:
        """Fail an agent that is irrecoverably idle."""
        if run.tmux_session_name:
            await self._tmux.kill_session(run.tmux_session_name)

        self._idle_detector.clear_state(run.id)
        await self._cleanup_handler.cleanup_agent(run, terminal_payload=f"Agent idle: {reason}")

    @staticmethod
    async def _read_recent_codex_response_items(
        transcript_path: str,
        *,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        def _read() -> list[dict[str, object]]:
            items: deque[dict[str, object]] = deque(maxlen=limit)
            with open(transcript_path, encoding="utf-8") as handle:
                for line_num, raw_line in enumerate(handle, start=1):
                    if not raw_line.strip():
                        continue
                    try:
                        data = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(data, dict) or data.get("type") != "response_item":
                        continue
                    payload = data.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    items.append(
                        {
                            "line_num": line_num,
                            "timestamp": data.get("timestamp"),
                            "payload_type": payload.get("type"),
                            "raw": data,
                        }
                    )
            return list(items)

        return await asyncio.to_thread(_read)

    async def _log_recent_codex_response_items(self, run: AgentRun, *, reason: str) -> None:
        session_manager = self._get_session_manager()
        if session_manager is None:
            return

        session_id = run.child_session_id or run.parent_session_id
        if not session_id:
            return

        try:
            session = await self._run_sqlite(session_manager.get, session_id)
        except Exception as exc:
            logger.warning(
                "Failed to load session %s for Codex idle diagnostics on run %s: %s",
                session_id,
                run.id,
                exc,
            )
            return

        if session is None or getattr(session, "source", None) != "codex":
            return

        transcript_path = getattr(session, "transcript_path", None)
        if not isinstance(transcript_path, str) or not transcript_path:
            logger.warning(
                "Codex idle diagnostic for run %s (%s): session %s has no transcript path",
                run.id,
                reason,
                session_id,
            )
            return

        try:
            items = await self._read_recent_codex_response_items(transcript_path)
        except OSError as exc:
            logger.warning(
                "Failed to read Codex transcript for idle diagnostic on run %s (%s): %s",
                run.id,
                reason,
                exc,
            )
            return

        if not items:
            logger.warning(
                "Codex idle diagnostic for run %s (%s): no recent response_item records for session %s",
                run.id,
                reason,
                session_id,
            )
            return

        logger.warning(
            "Codex idle diagnostic for run %s (%s) session %s: %s",
            run.id,
            reason,
            session_id,
            json.dumps(items, ensure_ascii=True),
        )
