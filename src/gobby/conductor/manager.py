"""ConductorManager — persistent tick-based orchestration agent.

The conductor is a ChatSession (haiku model) that receives cron ticks,
checks task/pipeline states, and dispatches dev/QA agents. Between ticks
it's idle; autocompress handles context growth.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.config.conductor import ConductorConfig
    from gobby.servers.chat_session import ChatSession
    from gobby.storage.cron_models import CronJob
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

CONDUCTOR_SYSTEM_PROMPT = """\
You are the Conductor — a persistent orchestration agent for the Gobby platform.

## Role
You receive periodic ticks and decide what work needs to happen. You are the brain
that keeps development moving forward.

## On Each Tick
1. Check task state: use `list_tasks` and `suggest_next_task` to find ready work
2. Check pipeline state: use `get_pipeline_status` to find stalled or waiting pipelines
3. Dispatch agents: use `spawn_agent` or `dispatch_batch` for ready tasks
4. Review completed pipeline executions when useful
5. Report: briefly summarize what you did or found

## Rules
- Be concise. You are a daemon, not a conversationalist.
- Only dispatch work when tasks are ready and there is no conflicting active work.
- If nothing needs attention, say "No action needed" and stop.
- Use progressive tool discovery: list_mcp_servers → list_tools → get_tool_schema → call_tool.
- Never dispatch agents for tasks that already have active agents.
"""


class ConductorManager:
    """Manages a persistent ChatSession that acts as the tick-based conductor.

    Implements the CronHandler interface (__call__) so it can be registered
    directly with CronExecutor.register_handler().
    """

    def __init__(
        self,
        project_id: str,
        project_path: str | None,
        session_manager: SessionManager,
        config: ConductorConfig,
        execution_manager: LocalPipelineExecutionManager | None = None,
    ) -> None:
        if config.provider != "claude":
            raise RuntimeError(
                f"Conductor provider '{config.provider}' is not supported. Use provider='claude'."
            )
        self._project_id = project_id
        self._project_path = project_path
        self._session_manager = session_manager
        self._config = config
        self._execution_manager = execution_manager
        self._session: ChatSession | None = None
        self._busy = False
        self._last_activity: datetime | None = None
        self._conversation_id = f"conductor-{project_id}"

    async def __call__(self, job: CronJob) -> str:
        """CronHandler interface — called by CronExecutor on each tick."""
        if self._config.skip_if_busy and self._busy:
            return "Conductor busy, skipping tick"
        return await self._handle_tick()

    async def _handle_tick(self) -> str:
        """Process a conductor tick."""
        self._busy = True
        try:
            session = await self._ensure_session()
            self._last_activity = datetime.now(UTC)
            tick_msg = (
                f"Conductor tick at {datetime.now(UTC).isoformat()}. "
                "Check tasks and pipelines. Dispatch agents if needed."
            )
            from gobby.llm.claude_models import DoneEvent, TextChunk

            parts: list[str] = []
            async for event in session.send_message(tick_msg):
                if isinstance(event, TextChunk):
                    parts.append(event.content)
                elif isinstance(event, DoneEvent):
                    break

            result = f"Conductor: {''.join(parts)[:500]}"

            # Review completed pipeline executions
            review_summary = await self._review_completed_pipelines()
            if review_summary:
                result += f" | {review_summary}"

            return result
        except Exception as e:
            logger.warning(f"Conductor tick failed: {e}")
            await self._destroy_session()
            return f"Conductor tick failed: {e}"
        finally:
            self._busy = False

    async def _review_completed_pipelines(self) -> str | None:
        """Review unreviewed terminal pipeline executions.

        Queries for completed/failed/cancelled executions without reviews,
        gathers structured data, sends to LLM for analysis, and stores
        the combined review. Caps at 5 reviews per tick.

        Returns:
            Summary string (e.g. "Reviewed 3 executions") or None if nothing to review.
        """
        if not self._execution_manager:
            return None

        try:
            unreviewed = self._execution_manager.get_unreviewed_completions(limit=5)
            if not unreviewed:
                return None

            import json

            from gobby.conductor.pipeline_review import (
                build_review_json,
                detect_patterns,
                format_review_prompt,
                gather_review_data,
            )

            reviewed_count = 0
            stored_reviews: list[dict[str, object]] = []

            for execution in unreviewed:
                try:
                    steps = self._execution_manager.get_steps_for_execution(execution.id)
                    data = gather_review_data(execution, steps)
                    prompt = format_review_prompt(data)

                    # Send review prompt to conductor LLM session
                    llm_analysis = await self._get_llm_review(prompt)

                    review_str = build_review_json(data, llm_analysis)
                    self._execution_manager.store_review(execution.id, review_str)
                    reviewed_count += 1

                    try:
                        stored_reviews.append(json.loads(review_str))
                    except json.JSONDecodeError:
                        pass

                except Exception as e:
                    logger.warning(f"Failed to review execution {execution.id}: {e}")

            # Detect cross-execution patterns and append to last review
            if len(stored_reviews) >= 2:
                patterns = detect_patterns(stored_reviews)
                if patterns and unreviewed:
                    last_execution = unreviewed[reviewed_count - 1]
                    try:
                        last_review = stored_reviews[-1]
                        last_review["cross_execution_patterns"] = patterns
                        self._execution_manager.store_review(
                            last_execution.id, json.dumps(last_review, default=str)
                        )
                    except Exception as e:
                        logger.debug(f"Failed to store cross-execution patterns: {e}")

            return f"Reviewed {reviewed_count} execution(s)" if reviewed_count else None

        except Exception as e:
            logger.warning(f"Pipeline review failed: {e}")
            return None

    async def _get_llm_review(self, prompt: str) -> dict[str, object] | None:
        """Send a review prompt to the conductor LLM and parse JSON response.

        Returns parsed JSON dict on success, None on failure (unparseable
        response, session error, etc.). Never raises.
        """
        import json

        if not self._session:
            return None

        try:
            from gobby.llm.claude_models import DoneEvent, TextChunk

            parts: list[str] = []
            async for event in self._session.send_message(prompt):
                if isinstance(event, TextChunk):
                    parts.append(event.content)
                elif isinstance(event, DoneEvent):
                    break

            response = "".join(parts).strip()

            # Strip markdown code fences if present
            if response.startswith("```"):
                lines = response.split("\n")
                # Remove first and last lines (fences)
                lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                response = "\n".join(lines)

            result: dict[str, object] = json.loads(response)
            return result
        except json.JSONDecodeError:
            logger.debug("LLM review response was not valid JSON")
            return None
        except Exception as e:
            logger.debug(f"LLM review request failed: {e}")
            return None

    async def _ensure_session(self) -> ChatSession:
        """Get or create the conductor ChatSession.

        Tears down idle sessions that haven't received a tick within
        idle_timeout_seconds, then creates a fresh one.
        """
        # Tear down idle sessions
        if self._session and self._last_activity:
            idle = (datetime.now(UTC) - self._last_activity).total_seconds()
            if idle > self._config.idle_timeout_seconds:
                logger.info(f"Conductor idle for {idle:.0f}s, tearing down session")
                await self._destroy_session()

        if self._session and self._session.is_connected:
            return self._session

        # Lazy import to avoid circular deps at module level
        from gobby.servers.chat_session import ChatSession

        session = ChatSession(
            conversation_id=self._conversation_id,
            provider=self._config.provider,
            project_id=self._project_id,
            project_path=self._project_path,
        )
        session.system_prompt_override = CONDUCTOR_SYSTEM_PROMPT

        # Register in DB for session tracking
        db_session = self._session_manager.register(
            external_id=self._conversation_id,
            machine_id="conductor",
            source="conductor",
            project_id=self._project_id,
        )
        session.db_session_id = db_session.id
        session.seq_num = db_session.seq_num
        session._session_manager_ref = self._session_manager

        await session.start(model=self._config.model)
        self._session = session
        logger.info(f"Conductor session created (model={self._config.model})")
        return session

    async def _destroy_session(self) -> None:
        """Tear down the current conductor session."""
        if self._session:
            try:
                await self._session.stop()
            except Exception as e:
                logger.debug(f"Error stopping conductor session: {e}")
            self._session = None

    async def shutdown(self) -> None:
        """Clean shutdown — called by GobbyRunner on stop."""
        await self._destroy_session()
        logger.info("Conductor shut down")
