"""
Hook Manager - Clean Coordinator for Claude Code Hooks.

This is the refactored HookManager that serves purely as a coordinator,
delegating all work to focused subsystems. It replaces the 5,774-line
God Object with a ~300-line routing layer.

Architecture:
    HookManager creates and coordinates subsystems:
    - Session-agnostic: DaemonClient, TranscriptProcessor
    - Session-scoped: SessionManager
    - Workflow-driven: RuleEngine + WorkflowHookHandler handle session lifecycle

Example:
    ```python
    from gobby.hooks.hook_manager import HookManager

    manager = HookManager(
        daemon_host="localhost",
        daemon_port=60887
    )

    result = manager.execute(
        hook_type="session-start",
        input_data={"external_id": "abc123", ...}
    )
    ```
"""

import asyncio
import logging
import os
import sqlite3
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.factory import HookManagerFactory

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

        # Setup logging first
        self.logger = self._setup_logging()

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
        )

        # Unpack all subsystems from factory components
        self._config = components.config
        self._database = components.database
        self._daemon_client = components.daemon_client
        self._transcript_processor = components.transcript_processor
        self._session_storage = components.session_storage
        self._session_task_manager = components.session_task_manager
        self._memory_storage = components.memory_storage
        self._message_manager = components.message_manager
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

    def _setup_logging(self) -> logging.Logger:
        """
        Setup structured logging with rotation.

        Returns:
            Configured logger instance
        """
        # Create logger
        logger = logging.getLogger("gobby.hooks")
        logger.setLevel(logging.DEBUG)

        # Avoid duplicate handlers if logger already configured
        if logger.handlers:
            return logger

        # File handler with rotation - use full path from config
        # Expand ~ to home directory before creating directories
        log_file_path = Path(self.log_file).expanduser()
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file_path,
            maxBytes=self.log_max_bytes,
            backupCount=self.log_backup_count,
        )
        file_handler.setLevel(logging.DEBUG)

        # Formatter with context
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(formatter)

        logger.addHandler(file_handler)

        return logger

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
        # Check daemon status (cached)
        is_ready, _, daemon_status, error_reason = self._get_cached_daemon_status()

        # Critical hooks that should retry before giving up
        # These hooks are essential for session context preservation
        critical_hooks = {
            HookEventType.SESSION_START,
            HookEventType.SESSION_END,
            HookEventType.PRE_COMPACT,
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

        # Resolve platform session_id from CLI external_id
        platform_session_id = self._session_lookup.resolve(event)

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
            try:
                response = handler(event)
            except Exception as e:
                self.logger.error(f"Event handler {event.event_type} failed: {e}", exc_info=True)
                return HookResponse(decision="allow", reason=f"Handler error: {e}")

            workflow_context, blocking_response = self._evaluate_workflow_rules(event)
            if blocking_response:
                return blocking_response

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

        # --- Hook-based transcript capture (Windsurf, Copilot) ---
        # These CLIs don't write local transcript files, so we
        # assemble transcripts from hook events as they flow through.
        if event.source in (SessionSource.WINDSURF, SessionSource.COPILOT) and platform_session_id:
            try:
                hook_messages = self._hook_assembler.process_event(platform_session_id, event)
                if hook_messages:
                    self._store_hook_messages(platform_session_id, hook_messages)
            except Exception as e:
                self.logger.warning(f"Hook transcript capture failed: {e}")
        # ---------------------------------------------------------

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

    def _evaluate_workflow_rules(self, event: HookEvent) -> tuple[str | None, HookResponse | None]:
        """Evaluate workflow rules and dispatch mcp_call effects.

        Args:
            event: The hook event to evaluate rules for.

        Returns:
            Tuple of (workflow_context, blocking_response).
            blocking_response is non-None if rules blocked/modified the event.
        """
        try:
            workflow_response = self._workflow_handler.handle(event)

            # Extract and dispatch mcp_calls BEFORE the block check —
            # they're explicit side effects that should fire regardless of decision
            mcp_calls = (workflow_response.metadata or {}).get("mcp_calls", [])
            self.logger.info(
                "Rule evaluation for %s: decision=%s, mcp_calls=%d",
                event.event_type,
                workflow_response.decision,
                len(mcp_calls),
            )

            if mcp_calls:
                self._dispatch_mcp_calls(mcp_calls, event)

            if workflow_response.decision != "allow":
                self.logger.info("Workflow blocked/modified event: %s", workflow_response.decision)
                return None, workflow_response

            # Capture context to merge later
            workflow_context = workflow_response.context if workflow_response.context else None

            return workflow_context, None

        except Exception as e:
            self.logger.error("Workflow evaluation failed: %s", e, exc_info=True)
            # Fail-open for workflow errors
            return None, None

    def _evaluate_blocking_webhooks(self, event: HookEvent) -> HookResponse | None:
        """Evaluate blocking webhooks before handler execution.

        Args:
            event: The hook event to evaluate webhooks for.

        Returns:
            HookResponse if a webhook blocked the event, None otherwise.
        """
        try:
            webhook_results = self._dispatch_webhooks_sync(event, blocking_only=True)
            decision, reason = self._webhook_dispatcher.get_blocking_decision(webhook_results)
            if decision == "block":
                self.logger.info(f"Webhook blocked event: {reason}")
                return HookResponse(decision="block", reason=reason or "Blocked by webhook")
        except Exception as e:
            self.logger.error(f"Blocking webhook dispatch failed: {e}", exc_info=True)
            # Fail-open for webhook errors
        return None

    def _dispatch_webhooks_sync(self, event: HookEvent, blocking_only: bool = False) -> list[Any]:
        """
        Dispatch webhooks synchronously (for blocking webhooks).

        Args:
            event: The hook event to dispatch.
            blocking_only: If True, only dispatch to blocking (can_block=True) endpoints.

        Returns:
            List of WebhookResult objects.
        """
        from gobby.hooks.webhooks import WebhookResult

        if not self._webhook_dispatcher.config.enabled:
            return []

        # Filter endpoints if blocking_only
        matching_endpoints = [
            ep
            for ep in self._webhook_dispatcher.config.endpoints
            if ep.enabled
            and self._webhook_dispatcher._matches_event(ep, event.event_type.value)
            and (not blocking_only or ep.can_block)
        ]

        if not matching_endpoints:
            return []

        # Build payload once
        payload = self._webhook_dispatcher._build_payload(event)

        # Run async dispatch in sync context
        async def dispatch_all() -> list[WebhookResult]:
            results: list[WebhookResult] = []
            for endpoint in matching_endpoints:
                result = await self._webhook_dispatcher._dispatch_single(endpoint, payload)
                results.append(result)
            return results

        # Execute in event loop
        try:
            asyncio.get_running_loop()
            # Already in async context - this method shouldn't be called here
            # Fall back to creating a new thread to run the coroutine synchronously
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, dispatch_all())
                return future.result()
        except RuntimeError:
            # Not in async context, run synchronously
            return asyncio.run(dispatch_all())

    def _dispatch_webhooks_async(self, event: HookEvent) -> None:
        """
        Dispatch non-blocking webhooks asynchronously (fire-and-forget).

        Args:
            event: The hook event to dispatch.
        """
        if not self._webhook_dispatcher.config.enabled:
            return

        # Filter to non-blocking endpoints only
        matching_endpoints = [
            ep
            for ep in self._webhook_dispatcher.config.endpoints
            if ep.enabled
            and self._webhook_dispatcher._matches_event(ep, event.event_type.value)
            and not ep.can_block
        ]

        if not matching_endpoints:
            return

        # Build payload
        payload = self._webhook_dispatcher._build_payload(event)

        async def dispatch_all() -> None:
            tasks = [
                self._webhook_dispatcher._dispatch_single(ep, payload) for ep in matching_endpoints
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

        # Fire and forget
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(dispatch_all())
        except RuntimeError:
            # No event loop, try using captured loop
            if self._loop:
                try:
                    asyncio.run_coroutine_threadsafe(dispatch_all(), self._loop)
                except Exception as e:
                    self.logger.warning(f"Failed to schedule async webhook: {e}")

    def _store_hook_messages(self, session_id: str, messages: list[Any]) -> None:
        """Store hook-assembled transcript messages asynchronously.

        Args:
            session_id: Platform session ID.
            messages: ParsedMessage objects from HookTranscriptAssembler.
        """
        coro = self._message_manager.store_messages(session_id, messages)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            if self._loop:
                try:
                    asyncio.run_coroutine_threadsafe(coro, self._loop)
                except Exception as e:
                    self.logger.warning(f"Failed to schedule hook message storage: {e}")
            else:
                # No event loop available — run synchronously as last resort
                self.logger.debug(
                    "No event loop available, running hook message storage synchronously"
                )
                try:
                    asyncio.run(coro)
                except Exception as e:
                    self.logger.warning(f"Sync hook message storage failed: {e}")

    def _dispatch_mcp_calls(self, mcp_calls: list[dict[str, Any]], event: HookEvent) -> None:
        """Dispatch mcp_call effects from rule engine evaluation.

        Injects event context (session_id, prompt_text) into each call's
        arguments and dispatches via ToolProxyService.

        Args:
            mcp_calls: List of mcp_call dicts from rule engine metadata.
                Each has: server, tool, arguments, background.
            event: The originating HookEvent (for context injection).
        """
        if not self.tool_proxy_getter:
            self.logger.debug("_dispatch_mcp_calls: no tool_proxy_getter, skipping")
            return

        self.logger.info(
            "_dispatch_mcp_calls: dispatching %d calls for %s",
            len(mcp_calls),
            event.event_type,
        )

        # Capture in local so mypy narrows past the None guard for closures
        _get_proxy = self.tool_proxy_getter

        for call in mcp_calls:
            server = call.get("server")
            tool = call.get("tool")
            arguments = dict(call.get("arguments") or {})
            background = call.get("background", False)

            if not server or not tool:
                self.logger.warning("_dispatch_mcp_calls: missing server or tool in %s", call)
                continue

            self.logger.info("_dispatch_mcp_calls: %s/%s (background=%s)", server, tool, background)

            # Inject event context into arguments
            if "session_id" not in arguments:
                arguments["session_id"] = event.metadata.get("_platform_session_id", "")
            if "prompt_text" not in arguments:
                arguments["prompt_text"] = event.data.get("prompt") if event.data else None

            async def _call(s: str, t: str, args: dict[str, Any]) -> None:
                try:
                    proxy = _get_proxy()
                    if not proxy:
                        self.logger.warning("_dispatch_mcp_calls: tool_proxy_getter returned None")
                        return
                    result = await proxy.call_tool(s, t, args, strip_unknown=True)
                    if isinstance(result, dict) and result.get("success") is False:
                        self.logger.warning(
                            "_dispatch_mcp_calls: %s/%s returned failure: %s",
                            s,
                            t,
                            result.get("error", "unknown"),
                        )
                except Exception as exc:
                    self.logger.error(
                        "_dispatch_mcp_calls: %s/%s failed: %s", s, t, exc, exc_info=True
                    )

            coro = _call(server, tool, arguments)

            if background:
                # Fire-and-forget (same pattern as broadcasting)
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(coro)
                except RuntimeError:
                    if self._loop and self._loop.is_running():
                        try:
                            asyncio.run_coroutine_threadsafe(coro, self._loop)
                        except Exception as e:
                            self.logger.warning(
                                "_dispatch_mcp_calls: failed to schedule %s/%s: %s",
                                server,
                                tool,
                                e,
                            )
                    else:
                        try:
                            asyncio.run(coro)
                        except Exception as e:
                            self.logger.warning(
                                "_dispatch_mcp_calls: background %s/%s failed: %s",
                                server,
                                tool,
                                e,
                            )
            else:
                # Blocking dispatch — must await completion, not fire-and-forget
                if self._loop and self._loop.is_running():
                    try:
                        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
                        future.result(timeout=30)
                    except Exception as e:
                        self.logger.error(
                            "_dispatch_mcp_calls: blocking %s/%s failed: %s",
                            server,
                            tool,
                            e,
                        )
                else:
                    try:
                        asyncio.run(coro)
                    except Exception as e:
                        self.logger.error(
                            "_dispatch_mcp_calls: blocking %s/%s failed: %s",
                            server,
                            tool,
                            e,
                        )

    def _resolve_summary_output_path(self, session_id: str) -> str:
        """Resolve session summary output directory from the session's project.

        Priority: project repo_path/.gobby/session_summaries > ~/.gobby/session_summaries

        Args:
            session_id: Platform session ID.

        Returns:
            Absolute path to the session_summaries directory.
        """
        fallback = "~/.gobby/session_summaries"
        try:
            session = self._session_storage.get(session_id)
            if session and session.project_id:
                from gobby.storage.projects import LocalProjectManager

                project_mgr = LocalProjectManager(self._database)
                project = project_mgr.get(session.project_id)
                if project and project.repo_path:
                    return str(Path(project.repo_path) / ".gobby" / "session_summaries")
        except Exception as e:
            self.logger.debug("_resolve_summary_output_path: fallback to global: %s", e)
        return fallback

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

        file_output_path = self._resolve_summary_output_path(session_id)

        async def _run() -> None:
            try:
                await generate_session_summaries(
                    session_id=session_id,
                    session_manager=self._session_storage,
                    llm_service=self._llm_service,
                    db=self._database,
                    write_file=True,
                    output_path=file_output_path,
                )
            except Exception as exc:
                self.logger.error(
                    "_dispatch_session_summaries: failed for session %s: %s: %s",
                    session_id,
                    type(exc).__name__,
                    exc,
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
                    self.logger.warning("_dispatch_session_summaries: failed to schedule: %s", e)
                    if done_event:
                        done_event.set()
            else:

                def _run_coro() -> None:
                    try:
                        asyncio.run(coro)
                    except Exception as e:
                        self.logger.warning("_dispatch_session_summaries: background failed: %s", e)
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

        # Get cwd or use current directory
        working_dir = Path(cwd) if cwd else Path.cwd()

        # Look up project from .gobby/project.json
        from gobby.utils.project_context import get_project_context

        project_context = get_project_context(working_dir)
        if project_context and project_context.get("id"):
            # Ensure project exists in database (may have been created on another machine)
            self._ensure_project_in_db(project_context)
            return str(project_context["id"])

        # No project.json found - use personal workspace
        from gobby.storage.projects import PERSONAL_PROJECT_ID

        self.logger.info(f"No project context for {working_dir}, using personal workspace")
        return PERSONAL_PROJECT_ID

    def _ensure_project_in_db(self, project_context: dict[str, Any]) -> None:
        """
        Ensure project from project.json exists in the database.

        This handles the case where project.json was created on another machine
        and the project ID doesn't exist in the local database.
        """
        if self._session_manager is None:
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
