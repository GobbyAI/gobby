"""Hook Manager - Coordinator for hook events.

Delegates dispatch work to :mod:`gobby.hooks.dispatchers` and event handling
to :mod:`gobby.hooks.event_handlers`.  See :class:`HookManager` for details.
"""

import asyncio
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from gobby.hooks.dispatchers.mcp import (
    dispatch_mcp_calls as _dispatch_mcp_calls_impl,
)
from gobby.hooks.dispatchers.mcp import (
    format_discovery_result as _format_discovery_result_impl,
)
from gobby.hooks.dispatchers.mcp import (
    proxy_self_call as _proxy_self_call_impl,
)
from gobby.hooks.dispatchers.mcp import (
    run_coro_blocking as _run_coro_blocking_impl,
)
from gobby.hooks.dispatchers.webhook import (
    dispatch_webhooks_async as _dispatch_webhooks_async_impl,
)
from gobby.hooks.dispatchers.webhook import (
    dispatch_webhooks_sync as _dispatch_webhooks_sync_impl,
)
from gobby.hooks.dispatchers.webhook import (
    evaluate_blocking_webhooks as _evaluate_blocking_webhooks_impl,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse
from gobby.hooks.factory import HookManagerFactory
from gobby.telemetry.tracing import create_span

if TYPE_CHECKING:
    from gobby.llm.service import LLMService


class HookManager:
    """
    Session-scoped coordinator for Claude Code hooks.

    Delegates all work to subsystems:
    - DaemonClient: HTTP communication with Gobby daemon
    - TranscriptProcessor: JSONL parsing and analysis
    - RuleEngine + WorkflowHookHandler: Handles session lifecycle and rule enforcement

    Session ID Mapping:
        There are two types of session IDs used throughout the system:

        | Name                     | Description                                    | Example                                |
        |--------------------------|------------------------------------------------|----------------------------------------|
        | external_id / session_id | CLI's internal session UUID (Claude Code, etc) | 683bc13e-091e-4911-9e59-e7546e385cd6   |
        | _platform_session_id     | Gobby's internal session.id (database PK)      | 0ebb2c00-0f58-4c39-9370-eba1833dec33   |

        The _platform_session_id is derived from session_manager.get_session_id(external_id, source)
        which looks up Gobby's session by the CLI's external_id.

        When injecting into agent context:
        - "session_id" in response.metadata = Gobby's _platform_session_id (for MCP tool calls)
        - "external_id" in response.metadata = CLI's session UUID (for transcript lookups)

    Attributes:
        daemon_host: Host for daemon communication
        daemon_port: Port for daemon communication
        log_file: Full path to log file
        logger: Configured logger instance
    """

    def __init__(
        self,
        daemon_host: str = "localhost",
        daemon_port: int = 60887,
        llm_service: "LLMService | None" = None,
        config: Any | None = None,
        log_file: str | None = None,
        log_max_bytes: int = 10 * 1024 * 1024,  # 10MB
        log_backup_count: int = 5,
        broadcaster: Any | None = None,
        tool_proxy_getter: Any | None = None,
        message_processor: Any | None = None,
        memory_sync_manager: Any | None = None,
        task_sync_manager: Any | None = None,
        code_index_trigger: Any | None = None,
    ):
        """
        Initialize HookManager with subsystems.

        Args:
            daemon_host: Daemon host for communication
            daemon_port: Daemon port for communication
            llm_service: Optional LLMService for multi-provider support
            config: Optional DaemonConfig instance for feature configuration
            log_file: Full path to log file (default: ~/.gobby/logs/hook-manager.log)
            log_max_bytes: Max log file size before rotation
            log_backup_count: Number of backup log files
            broadcaster: Optional HookEventBroadcaster instance
            tool_proxy_getter: Callable returning ToolProxyService (lazy getter)
            message_processor: SessionMessageProcessor instance
            memory_sync_manager: Optional MemorySyncManager instance
            task_sync_manager: Optional TaskSyncManager instance
        """
        self.daemon_host = daemon_host
        self.daemon_port = daemon_port
        self.daemon_url = f"http://{daemon_host}:{daemon_port}"
        gobby_home = os.environ.get("GOBBY_HOME", str(Path.home() / ".gobby"))
        self.log_file = log_file or str(Path(gobby_home) / "logs" / "hook-manager.log")
        self.log_max_bytes = log_max_bytes
        self.log_backup_count = log_backup_count
        self.broadcaster = broadcaster
        self.tool_proxy_getter = tool_proxy_getter
        self._message_processor = message_processor
        self.memory_sync_manager = memory_sync_manager
        self.task_sync_manager = task_sync_manager

        # Capture event loop for thread-safe broadcasting (if running in async context)
        self._loop: asyncio.AbstractEventLoop | None
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        # Setup logging
        self.logger = logging.getLogger("gobby.hooks")

        # Store LLM service
        self._llm_service = llm_service

        # Track sessions that have received full metadata injection
        # Key: "{platform_session_id}:{source}" - cleared on daemon restart
        self._injected_sessions: set[str] = set()

        # Create all subsystems via factory
        components = HookManagerFactory.create(
            daemon_host=daemon_host,
            daemon_port=daemon_port,
            llm_service=llm_service,
            config=config,
            hook_logger=self.logger,
            loop=self._loop,
            broadcaster=broadcaster,
            tool_proxy_getter=tool_proxy_getter,
            message_processor=message_processor,
            memory_sync_manager=memory_sync_manager,
            task_sync_manager=task_sync_manager,
            get_machine_id=self.get_machine_id,
            resolve_project_id=self._resolve_project_id,
            code_index_trigger=code_index_trigger,
        )

        # Unpack all subsystems from factory components
        self._config = components.config
        self._database = components.database
        self._daemon_client = components.daemon_client
        self._transcript_processor = components.transcript_processor
        self._session_storage = components.session_storage
        self._session_task_manager = components.session_task_manager
        self._memory_storage = components.memory_storage
        self._task_manager = components.task_manager
        self._agent_run_manager = components.agent_run_manager
        self._worktree_manager = components.worktree_manager
        self._stop_registry = components.stop_registry
        self._progress_tracker = components.progress_tracker
        self._stuck_detector = components.stuck_detector
        self._memory_manager = components.memory_manager
        self._workflow_loader = components.workflow_loader
        self._skill_manager = components.skill_manager
        self._pipeline_executor = components.pipeline_executor
        self._workflow_handler = components.workflow_handler
        self._webhook_dispatcher = components.webhook_dispatcher
        self._session_manager = components.session_manager
        self._session_coordinator = components.session_coordinator
        self._health_monitor = components.health_monitor
        self._hook_assembler = components.hook_assembler
        self._event_handlers = components.event_handlers

        # Wire callback for session summary generation (method lives on HookManager,
        # called from EventHandlers mixins during session-end and before-agent).
        self._event_handlers._dispatch_session_summaries_fn = self._dispatch_session_summaries

        # Inter-session message manager (for web chat -> CLI piggyback delivery)
        from gobby.storage.inter_session_messages import InterSessionMessageManager

        self._inter_session_msg_manager: InterSessionMessageManager | None = None
        if self._database:
            try:
                self._inter_session_msg_manager = InterSessionMessageManager(self._database)
            except Exception as e:
                self.logger.warning(f"Failed to create InterSessionMessageManager: {e}")

        # Response metadata enrichment service
        from gobby.hooks.event_enrichment import EventEnricher

        self._enricher = EventEnricher(
            session_storage=self._session_storage,
            injected_sessions=self._injected_sessions,
            inter_session_msg_manager=self._inter_session_msg_manager,
        )

        # Session lookup service (resolves platform session IDs from CLI external IDs)
        from gobby.hooks.session_lookup import SessionLookupService

        self._session_lookup = SessionLookupService(
            session_manager=self._session_manager,
            session_coordinator=self._session_coordinator,
            session_task_manager=self._session_task_manager,
            get_machine_id=self.get_machine_id,
            resolve_project_id=self._resolve_project_id,
            logger=self.logger,
        )

        # Start background health check monitoring
        self._start_health_check_monitoring()

        # Re-register active sessions with message processor (after daemon restart)
        self._reregister_active_sessions()

        self.logger.debug("HookManager initialized")

    def _reregister_active_sessions(self) -> None:
        """
        Re-register active sessions with the message processor.

        Called during HookManager initialization to restore message processing
        for sessions that were active before a daemon restart.
        """
        self._session_coordinator.reregister_active_sessions()

    def _start_health_check_monitoring(self) -> None:
        """Start background daemon health check monitoring."""
        self._health_monitor.start()

    def _get_cached_daemon_status(self) -> tuple[bool, str | None, str, str | None]:
        """
        Get cached daemon status without making HTTP call.

        Returns:
            Tuple of (is_ready, message, status, error)
        """
        return self._health_monitor.get_cached_status()

    def handle(self, event: HookEvent) -> HookResponse:
        """
        Handle unified HookEvent from any CLI source.

        This is the main entry point for hook handling. Adapters translate
        CLI-specific payloads to HookEvent and call this method.

        Args:
            event: Unified HookEvent with event_type, session_id, source, and data.

        Returns:
            HookResponse with decision, context, and reason fields.

        Raises:
            ValueError: If event_type has no registered handler.
        """
        with create_span(
            "hook.handle",
            attributes={
                "event_type": str(event.event_type),
                "source": str(event.source),
            },
        ) as span:
            try:
                response = self._handle_internal(event)
                if span.is_recording():
                    span.set_attribute("decision", response.decision)
                return response
            except Exception as e:
                if span.is_recording():
                    span.record_exception(e)
                raise

    def _handle_internal(self, event: HookEvent) -> HookResponse:
        """Internal handle logic wrapped by span."""
        # Check daemon status (cached)
        is_ready, _, daemon_status, error_reason = self._get_cached_daemon_status()

        # Critical hooks that should retry before giving up
        # These hooks are essential for session context preservation
        critical_hooks = {
            HookEventType.SESSION_START,
            HookEventType.SESSION_END,
            HookEventType.PRE_COMPACT,
            HookEventType.AFTER_AGENT,
            HookEventType.STOP,
        }
        retry_delays = [0.5, 1.0, 2.0]  # Exponential backoff

        # Retry with fresh health checks for critical hooks
        if not is_ready and event.event_type in critical_hooks:
            for attempt, delay in enumerate(retry_delays, 1):
                time.sleep(delay)
                is_ready = self._health_monitor.check_now()
                if is_ready:
                    self.logger.info(
                        f"Daemon recovered after {attempt} retry(ies) for {event.event_type}"
                    )
                    break
                self.logger.debug(
                    f"Daemon still unavailable, retry {attempt}/{len(retry_delays)} "
                    f"for {event.event_type}"
                )

        if not is_ready:
            self.logger.warning(
                f"Daemon not available after retries, skipping hook execution: {event.event_type}. "
                f"Status: {daemon_status}, Error: {error_reason}"
            )
            return HookResponse(
                decision="allow",  # Fail-open
                reason=f"Daemon {daemon_status}: {error_reason or 'Unknown'}",
            )

        # SESSION_START is special: the handler establishes the canonical
        # platform session first (including pre-created web-chat rows). Doing a
        # generic lookup here can auto-register a stray duplicate before the
        # handler gets a chance to bind the real session.
        if event.event_type == HookEventType.SESSION_START:
            if not event.project_id:
                cwd = event.cwd or event.data.get("cwd")
                event.project_id = self._resolve_project_id(event.data.get("project_id"), cwd)
        else:
            # Resolve platform session_id from CLI external_id
            self._session_lookup.resolve(event)  # side-effect: enriches event.metadata

        # Translate #N session references to UUIDs for MCP tool calls.
        # #N is human-friendly but ambiguous across projects (seq_num is per-project).
        # The hook has project context; the MCP server doesn't. Resolve here so
        # downstream tools get unambiguous UUIDs.
        self._resolve_session_refs_in_tool_input(event)

        # Get handler for this event type
        handler = self._get_event_handler(event.event_type)
        if handler is None:
            self.logger.warning(f"No handler for event type: {event.event_type}")
            return HookResponse(decision="allow")  # Fail-open for unknown events

        # --- Evaluate rules and execute handler ---
        # For SESSION_START: run handler first to register the session and set
        # _platform_session_id, then evaluate rules with the correct session ID.
        # This ensures set_variable effects are stored under the platform session_id
        # rather than the CLI's external_id.
        # For all other events: evaluate rules first so block effects can prevent
        # handler execution.
        if event.event_type == HookEventType.SESSION_START:
            with create_span("hook.session_start.handler"):
                try:
                    response = handler(event)
                except Exception as e:
                    self.logger.error(
                        f"Event handler {event.event_type} failed: {e}", exc_info=True
                    )
                    return HookResponse(decision="allow", reason=f"Handler error: {e}")

            with create_span("hook.session_start.rules"):
                workflow_context, blocking_response = self._evaluate_workflow_rules(event)
                if blocking_response:
                    return blocking_response

            with create_span("hook.session_start.webhooks"):
                webhook_block = self._evaluate_blocking_webhooks(event)
                if webhook_block:
                    return webhook_block
        else:
            workflow_context, blocking_response = self._evaluate_workflow_rules(event)
            if blocking_response:
                return blocking_response

            webhook_block = self._evaluate_blocking_webhooks(event)
            if webhook_block:
                return webhook_block

            try:
                response = handler(event)
            except Exception as e:
                self.logger.error(f"Event handler {event.event_type} failed: {e}", exc_info=True)
                return HookResponse(decision="allow", reason=f"Handler error: {e}")

        # --- Common post-processing ---

        # Auto-coerce stringified arguments in call_tool (universal fix, not rule-gated)
        if event.data.pop("_input_coerced", False):
            event.metadata.setdefault("_modified_input", event.data.get("tool_input", {}))
            event.metadata.setdefault("_auto_approve", True)

        # Propagate rewrite_input from rule evaluation to response (PreToolUse)
        if "_modified_input" in event.metadata:
            response.modified_input = event.metadata.pop("_modified_input")
            response.auto_approve = event.metadata.pop("_auto_approve", False)

        # If we resolved #N session refs but no rule/coercion set _modified_input,
        # create one so Claude Code sends UUIDs to the MCP server
        if event.metadata.pop("_session_refs_resolved", False):
            if not response.modified_input:
                response.modified_input = event.data.get("tool_input", {})
                response.auto_approve = True

        with create_span("hook.enrich"):
            try:
                self._enricher.enrich(event, response, workflow_context=workflow_context)
            except Exception as e:
                self.logger.error(f"Response enrichment failed: {e}", exc_info=True)

        # Broadcast event (fire-and-forget)
        if self.broadcaster:
            try:
                # Case 1: Running in an event loop (e.g. from app-server client)
                loop = asyncio.get_running_loop()
                loop.create_task(self.broadcaster.broadcast_event(event, response))
            except RuntimeError:
                # Case 2: Running in a thread (e.g. from HTTP endpoint via to_thread)
                if self._loop:
                    try:
                        # Use the main loop captured at init
                        asyncio.run_coroutine_threadsafe(
                            self.broadcaster.broadcast_event(event, response),
                            self._loop,
                        )
                    except Exception as e:
                        self.logger.warning(f"Failed to schedule broadcast threadsafe: {e}")
                else:
                    self.logger.debug("No event loop available for broadcasting")

        # Dispatch non-blocking webhooks (fire-and-forget)
        try:
            self._dispatch_webhooks_async(event)
        except Exception as e:
            self.logger.warning(f"Non-blocking webhook dispatch failed: {e}")

        # Hook-based transcript capture removed (session_messages table dropped)

        return cast(HookResponse, response)

    def _get_event_handler(self, event_type: HookEventType) -> Any | None:
        """
        Get the handler method for a given HookEventType.

        Args:
            event_type: The unified event type enum value.

        Returns:
            Handler method or None if not found.
        """
        return self._event_handlers.get_handler(event_type)

    def _resolve_session_refs_in_tool_input(self, event: HookEvent) -> None:
        """Resolve #N session references to UUIDs in MCP tool arguments.

        Modifies event.data["tool_input"] in place so all downstream consumers
        (rules, handlers, auto-coercion) see resolved UUIDs. Sets a flag so
        post-processing knows to create _modified_input if no one else did.
        """
        if event.event_type != HookEventType.BEFORE_TOOL:
            return

        tool_name = (event.data or {}).get("tool_name", "")
        if not tool_name.startswith("mcp__gobby__"):
            return

        tool_input = event.data.get("tool_input")
        if not tool_input or not isinstance(tool_input, dict):
            return

        project_id = event.project_id
        modified = False

        # Top-level session_id (set_variable, get_variable, call_tool, etc.)
        modified |= self._try_resolve_session_field(tool_input, "session_id", project_id)

        # Nested session_id inside call_tool arguments
        if tool_name == "mcp__gobby__call_tool":
            args = tool_input.get("arguments")
            if isinstance(args, dict):
                modified |= self._try_resolve_session_field(args, "session_id", project_id)

        if modified:
            event.metadata["_session_refs_resolved"] = True

    def _try_resolve_session_field(
        self, d: dict[str, Any], field: str, project_id: str | None
    ) -> bool:
        """Resolve a #N session reference in d[field] to UUID in place.

        Returns True if the field was rewritten.
        """
        val = d.get(field)
        if not isinstance(val, str):
            return False

        ref = val.lstrip("#") if val.startswith("#") else val
        if not ref.isdigit():
            return False  # Already a UUID or prefix — no resolution needed

        try:
            resolved = self._session_manager.resolve_session_reference(val, project_id)
            if resolved != val:
                d[field] = resolved
                return True
        except ValueError as e:
            self.logger.debug(f"Could not resolve session ref '{val}': {e}")
        except Exception as e:
            self.logger.warning(
                f"Unexpected error resolving session ref '{val}': {e}", exc_info=True
            )

        return False

    @staticmethod
    def _summarize_mcp_calls(mcp_calls: list[dict[str, Any]]) -> list[str]:
        """Return compact server/tool labels for workflow-triggered MCP calls."""
        targets: list[str] = []
        for call in mcp_calls:
            server = call.get("server")
            tool = call.get("tool")
            if isinstance(server, str) and isinstance(tool, str) and server and tool:
                targets.append(f"{server}/{tool}")
        return targets

    def _log_workflow_evaluation(
        self,
        event: HookEvent,
        workflow_response: HookResponse,
        mcp_calls: list[dict[str, Any]],
    ) -> None:
        """Log workflow decisions, keeping routine allow decisions at debug level."""
        session_id = event.metadata.get("_platform_session_id", "unknown")
        event_name = event.event_type.value
        tool_name = event.data.get("tool_name")
        has_rewrite = bool(workflow_response.modified_input)
        has_visible_side_effects = bool(mcp_calls) or has_rewrite or workflow_response.auto_approve

        parts = [
            f"Workflow rule evaluation: event={event_name}",
            f"decision={workflow_response.decision}",
            f"session={session_id}",
        ]
        if isinstance(tool_name, str) and tool_name:
            parts.append(f"tool={tool_name}")
        if mcp_calls:
            parts.append(f"mcp_calls={len(mcp_calls)}")
            mcp_targets = self._summarize_mcp_calls(mcp_calls)
            if mcp_targets:
                parts.append(f"mcp_targets={', '.join(mcp_targets)}")
        if has_rewrite:
            parts.append("rewrote_input=true")
        if workflow_response.auto_approve:
            parts.append("auto_approve=true")
        if workflow_response.reason and workflow_response.decision != "allow":
            parts.append(f"reason={workflow_response.reason}")

        message = ", ".join(parts)
        if workflow_response.decision != "allow" or has_visible_side_effects:
            self.logger.info(message)
        else:
            self.logger.debug(message)

    def _evaluate_workflow_rules(self, event: HookEvent) -> tuple[str | None, HookResponse | None]:
        """Evaluate workflow rules and dispatch mcp_call effects.

        Args:
            event: The hook event to evaluate rules for.

        Returns:
            Tuple of (workflow_context, blocking_response).
            blocking_response is non-None if rules blocked/modified the event.
        """
        try:
            with create_span("hook.rules.evaluate"):
                workflow_response = self._workflow_handler.handle(event)

            # Extract and dispatch mcp_calls BEFORE the block check —
            # they're explicit side effects that should fire regardless of decision
            mcp_calls = (workflow_response.metadata or {}).get("mcp_calls", [])

            with create_span(
                "hook.rules.mcp_dispatch",
                attributes={
                    "mcp_call_count": len(mcp_calls),
                    "mcp_calls": [f"{c.get('server')}/{c.get('tool')}" for c in mcp_calls],
                },
            ):
                dispatch_results = self._dispatch_mcp_calls(mcp_calls, event) if mcp_calls else []

            # Process auto-heal dispatch results: inject context, block on failure/success
            extra_context: list[str] = []
            block_override: HookResponse | None = None

            session_id = event.metadata.get("_platform_session_id")

            for dr in dispatch_results:
                if dr.get("inject_result") and dr.get("result"):
                    # Dedup memory injection — filter already-injected memories
                    if dr.get("tool") == "search_memories" and session_id:
                        dr["result"] = self._dedup_memory_results(dr["result"], session_id)
                    # Dedup skill suggestions — filter already-suggested and low-relevance
                    if dr.get("tool") == "search_skills" and session_id:
                        dr["result"] = self._dedup_skill_results(dr["result"], session_id)
                    extra_context.append(self._format_discovery_result(dr))
                if dr.get("block_on_failure") and not dr.get("success"):
                    result = dr.get("result") or {}
                    error_msg = (
                        result.get("error", "unknown") if isinstance(result, dict) else str(result)
                    )
                    block_override = HookResponse(
                        decision="block",
                        reason=(
                            f"Auto-heal prerequisite failed: {dr['server']}/{dr['tool']}: "
                            f"{error_msg}"
                        ),
                        context="\n\n".join(extra_context) if extra_context else None,
                    )
                    break
                if dr.get("block_on_success") and dr.get("success"):
                    block_override = HookResponse(
                        decision="block",
                        reason=(
                            f"Intercepted by {dr['server']}/{dr['tool']} — results injected below."
                        ),
                        context="\n\n".join(extra_context) if extra_context else None,
                    )
                    break

            if block_override:
                self._log_workflow_evaluation(event, block_override, mcp_calls)
                return None, block_override

            if workflow_response.decision != "allow":
                self._log_workflow_evaluation(event, workflow_response, mcp_calls)
                # Merge any auto-heal context into the block response
                if extra_context and workflow_response.context:
                    workflow_response.context = (
                        workflow_response.context + "\n\n" + "\n\n".join(extra_context)
                    )
                elif extra_context:
                    workflow_response.context = "\n\n".join(extra_context)
                return None, workflow_response

            # Stash rewrite_input data on event.metadata
            # so the main handle() method can propagate them to the final response
            if workflow_response.modified_input:
                event.metadata["_modified_input"] = workflow_response.modified_input
                event.metadata["_auto_approve"] = workflow_response.auto_approve

            self._log_workflow_evaluation(event, workflow_response, mcp_calls)

            # Capture context to merge later
            workflow_context = workflow_response.context if workflow_response.context else None
            # Merge auto-heal discovery context
            if extra_context:
                heal_context = "\n\n".join(extra_context)
                workflow_context = (
                    f"{workflow_context}\n\n{heal_context}" if workflow_context else heal_context
                )

            return workflow_context, None

        except Exception as e:
            self.logger.error(f"Workflow evaluation failed: {e}", exc_info=True)
            # Fail-open for workflow errors
            return None, None

    def _evaluate_blocking_webhooks(self, event: HookEvent) -> HookResponse | None:
        """Evaluate blocking webhooks before handler execution."""
        return _evaluate_blocking_webhooks_impl(
            event, self._webhook_dispatcher, self.logger, self._loop
        )

    def _dispatch_webhooks_sync(self, event: HookEvent, blocking_only: bool = False) -> list[Any]:
        """Dispatch webhooks synchronously (for blocking webhooks)."""
        return _dispatch_webhooks_sync_impl(
            event, self._webhook_dispatcher, self.logger, blocking_only
        )

    def _dispatch_webhooks_async(self, event: HookEvent) -> None:
        """Dispatch non-blocking webhooks asynchronously (fire-and-forget)."""
        _dispatch_webhooks_async_impl(event, self._webhook_dispatcher, self.logger, self._loop)

    def _dispatch_mcp_calls(
        self, mcp_calls: list[dict[str, Any]], event: HookEvent
    ) -> list[dict[str, Any]]:
        """Dispatch mcp_call effects from rule engine evaluation."""
        return _dispatch_mcp_calls_impl(
            mcp_calls, event, self.tool_proxy_getter, self._loop, self.logger
        )

    def _run_coro_blocking(self, coro: Any) -> Any:
        """Run a coroutine blocking, using the best available event loop strategy."""
        return _run_coro_blocking_impl(coro, self._loop, self.logger)

    async def _proxy_self_call(self, proxy: Any, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Route _proxy/* tool calls to ToolProxyService methods directly."""
        return await _proxy_self_call_impl(proxy, tool, args)

    @staticmethod
    def _format_discovery_result(dr: dict[str, Any]) -> str:
        """Format a proxy discovery result for context injection."""
        return _format_discovery_result_impl(dr)

    def _dedup_memory_results(self, result: dict[str, Any], session_id: str) -> dict[str, Any]:
        """Filter already-injected memories and track newly-injected IDs.

        Reads ``injected_memory_ids`` from session variables, removes memories
        whose IDs are already present, then appends the remaining IDs back to
        the variable for future dedup.  Fails open — returns unfiltered result
        on any error.
        """
        try:
            from gobby.workflows.state_manager import SessionVariableManager

            sv_mgr = SessionVariableManager(self._database)
            variables = sv_mgr.get_variables(session_id)
            already_injected: set[str] = set(variables.get("injected_memory_ids", []))

            memories = result.get("memories", [])
            id_less = [m for m in memories if not m.get("id")]
            if id_less:
                self.logger.warning(
                    "Memory dedup: %d memories lack 'id' field and cannot be tracked",
                    len(id_less),
                )
            if not memories or not already_injected:
                # Nothing to filter — but still track the IDs we're about to inject
                new_ids = [m["id"] for m in memories if m.get("id")]
                if new_ids:
                    sv_mgr.append_to_set_variable(session_id, "injected_memory_ids", new_ids)
                return result

            filtered = [m for m in memories if m.get("id") not in already_injected]
            new_ids = [m["id"] for m in filtered if m.get("id")]
            if new_ids:
                sv_mgr.append_to_set_variable(session_id, "injected_memory_ids", new_ids)

            return {**result, "memories": filtered}
        except Exception as e:
            self.logger.debug(f"Memory injection dedup failed (fail-open): {e}")
            return result

    def _dedup_skill_results(self, result: dict[str, Any], session_id: str) -> dict[str, Any]:
        """Filter already-suggested skills and low-relevance results.

        Reads ``suggested_skill_names`` from session variables, removes skills
        whose names are already present or whose relevance score is below the
        threshold, then appends the remaining names back for future dedup.
        Fails open — returns unfiltered result on any error.
        """
        _MIN_RELEVANCE = 0.65

        try:
            from gobby.workflows.state_manager import SessionVariableManager

            sv_mgr = SessionVariableManager(self._database)
            variables = sv_mgr.get_variables(session_id)
            already_suggested: set[str] = set(variables.get("suggested_skill_names", []))

            results_list = result.get("results", [])
            if not results_list:
                return result

            # Filter by relevance threshold and dedup
            filtered = [
                r
                for r in results_list
                if r.get("score", 0) >= _MIN_RELEVANCE
                and r.get("skill_name", "") not in already_suggested
            ]

            # Track newly suggested skill names
            new_names = [r["skill_name"] for r in filtered if r.get("skill_name")]
            if new_names:
                sv_mgr.append_to_set_variable(session_id, "suggested_skill_names", new_names)

            return {**result, "results": filtered, "count": len(filtered)}
        except Exception as e:
            self.logger.debug(f"Skill suggestion dedup failed (fail-open): {e}")
            return result

    def _dispatch_session_summaries(
        self, session_id: str, background: bool = False, done_event: threading.Event | None = None
    ) -> None:
        """Fire session summary generation.

        Uses the shared generate_session_summaries() which reads the full
        transcript, runs TranscriptAnalyzer + LLM, and persists results.

        Always dispatched as background (fire-and-forget) — the background
        param is kept for interface compat but is now ignored. SESSION_START
        polls for the result instead of blocking here, which avoids the
        previous 30s timeout bug when LLM calls took longer.

        Args:
            session_id: Platform session ID.
            background: Ignored — always runs in background.
        """
        from gobby.sessions.summarize import generate_session_summaries

        async def _run() -> None:
            try:
                await generate_session_summaries(
                    session_id=session_id,
                    session_manager=self._session_storage,
                    llm_service=self._llm_service,
                    db=self._database,
                )
            except Exception as exc:
                self.logger.error(
                    f"_dispatch_session_summaries: failed for session {session_id}: {type(exc).__name__}: {exc}",
                    exc_info=True,
                )
            finally:
                if done_event:
                    done_event.set()

        coro = _run()

        # Always fire-and-forget onto event loop
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            if self._loop and self._loop.is_running():
                try:
                    asyncio.run_coroutine_threadsafe(coro, self._loop)
                except Exception as e:
                    coro.close()  # Prevent "coroutine never awaited" warning
                    self.logger.warning(f"_dispatch_session_summaries: failed to schedule: {e}")
                    if done_event:
                        done_event.set()
            else:

                def _run_coro() -> None:
                    try:
                        asyncio.run(coro)
                    except Exception as e:
                        self.logger.warning(f"_dispatch_session_summaries: background failed: {e}")
                        if done_event:
                            done_event.set()

                threading.Thread(target=_run_coro, daemon=True).start()

    def shutdown(self) -> None:
        """
        Clean up HookManager resources on daemon shutdown.

        Stops background health check monitoring and transcript watchers.
        """
        self.logger.debug("HookManager shutting down")

        # Stop health check monitoring (delegated to HealthMonitor)
        self._health_monitor.stop()

        # Close webhook dispatcher HTTP client
        try:
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._webhook_dispatcher.close(), self._loop
                ).result(timeout=5.0)
            else:
                asyncio.run(self._webhook_dispatcher.close())
        except Exception as e:
            self.logger.warning(f"Failed to close webhook dispatcher: {e}")

        if hasattr(self, "_database"):
            self._database.close()

        self.logger.debug("HookManager shutdown complete")

    # ==================== HELPER METHODS ====================

    def get_machine_id(self) -> str:
        """Get unique machine identifier."""
        from gobby.utils.machine_id import get_machine_id as _get_machine_id

        result = _get_machine_id()
        return result or "unknown-machine"

    def _resolve_project_id(self, project_id: str | None, cwd: str | None) -> str:
        """
        Resolve project_id from cwd if not provided.

        If project_id is given, returns it directly.
        Otherwise, looks up project from .gobby/project.json in the cwd.
        If no project.json exists, automatically initializes the project.

        Args:
            project_id: Optional explicit project ID
            cwd: Current working directory path

        Returns:
            Project ID (existing or newly created)
        """
        if project_id:
            return project_id

        if not cwd:
            # No CWD available (e.g. daemon startup, factory init).
            # Fall back to personal workspace since there's no project context.
            from gobby.storage.projects import PERSONAL_PROJECT_ID

            return PERSONAL_PROJECT_ID

        working_dir = Path(cwd)

        # Look up project from .gobby/project.json
        from gobby.utils.project_context import get_project_context

        project_context = get_project_context(working_dir)
        if project_context and project_context.get("id"):
            # Ensure project exists in database (may have been created on another machine)
            self._ensure_project_in_db(project_context)
            return str(project_context["id"])

        raise ValueError(
            f"No .gobby/project.json found in {working_dir}. "
            f"Run 'gobby init' in your project directory first."
        )

    def _ensure_project_in_db(self, project_context: dict[str, Any]) -> None:
        """
        Ensure project from project.json exists in the database.

        This handles the case where project.json was created on another machine
        and the project ID doesn't exist in the local database.
        """
        if not hasattr(self, "_session_manager") or self._session_manager is None:
            return

        from gobby.storage.projects import LocalProjectManager

        project_id = str(project_context["id"])
        project_name = project_context.get("name", "unknown")
        repo_path = project_context.get("project_path")

        try:
            db = self._session_manager.db
            project_manager = LocalProjectManager(db)
            project_manager.ensure_exists(project_id, project_name, repo_path)
        except (sqlite3.Error, ValueError, RuntimeError) as e:
            self.logger.warning(f"Failed to ensure project in database: {e}")
