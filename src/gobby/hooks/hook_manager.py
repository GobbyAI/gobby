"""Hook Manager - Coordinator for hook events.

Delegates dispatch work to :mod:`gobby.hooks.dispatchers` and event handling
to :mod:`gobby.hooks.event_handlers`.  See :class:`HookManager` for details.
"""

import asyncio
import concurrent.futures
import copy
import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import psycopg

from gobby.hooks.agent_run_ingress import (
    TERMINAL_INGRESS_HOOK_TYPES,
    validate_managed_agent_hook,
)
from gobby.hooks.broadcaster import schedule_hook_broadcast
from gobby.hooks.dispatchers import mcp as mcp_dispatcher
from gobby.hooks.dispatchers import webhook as webhook_dispatcher
from gobby.hooks.effect_deadline import new_blocking_effect_deadline
from gobby.hooks.events import HookEvent, HookEventType, HookResponse
from gobby.hooks.factory import HookManagerFactory
from gobby.hooks.health_gate import ensure_daemon_ready, ensure_daemon_ready_async
from gobby.hooks.project_context import ProjectIdResolver, resolve_hook_project_context
from gobby.hooks.rule_evaluator import WorkflowRuleEvaluator
from gobby.hooks.session_activation import reconcile_session_activation
from gobby.hooks.session_ref_resolution import (
    resolve_session_refs_in_tool_input,
)
from gobby.hooks.session_summary_dispatcher import SessionSummaryDispatcher
from gobby.hooks.session_types import HookSessionManager
from gobby.sessions.activity import record_session_activity
from gobby.storage.machines import LocalMachineManager
from gobby.telemetry.tracing import create_span
from gobby.utils.session_refs import try_resolve_session_field

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.config.values import ConfigRuntimeReader
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.hooks.event_handlers import EventHandlers
    from gobby.llm.service import LLMService
    from gobby.memory.manager import MemoryManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager


def _hook_text_field(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class HookManager:
    """Session-scoped coordinator for hook events."""

    def __init__(
        self,
        daemon_host: str = "localhost",
        daemon_port: int = 60887,
        llm_service: "LLMService | None" = None,
        config: Any | None = None,
        broadcaster: Any | None = None,
        tool_proxy_getter: Any | None = None,
        message_processor_resolver: Callable[[], Any | None] | None = None,
        agent_runner: "AgentRunner | None" = None,
        completion_registry: "CompletionEventRegistry | None" = None,
        config_runtime: "ConfigRuntimeReader | None" = None,
        database: "HubDatabase | None" = None,
        session_manager: "SessionManager | None" = None,
        code_index_trigger: Any | None = None,
        memory_manager: "MemoryManager | None" = None,
        terminal_manager: Any | None = None,
    ) -> None:
        self.daemon_host = daemon_host
        self.daemon_port = daemon_port
        self.daemon_url = f"http://{daemon_host}:{daemon_port}"
        self.broadcaster = broadcaster
        self.tool_proxy_getter = tool_proxy_getter
        self._completion_registry = completion_registry
        self._owns_database = database is None and session_manager is None
        self._shutdown_complete = False

        # Capture event loop for thread-safe broadcasting (if running in async context)
        self._loop: asyncio.AbstractEventLoop | None
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        # Setup logging
        self.logger = logging.getLogger("gobby.hooks")
        self._project_id_resolver = ProjectIdResolver(
            logger=self.logger,
            ensure_project_in_db=lambda context: self._ensure_project_in_db(context),
        )

        # Store the creation-time LLM service and the runtime used to resolve
        # the current epoch's replacement per event.
        self._llm_service = llm_service
        self._config_runtime = config_runtime

        # Track sessions that have received full metadata injection
        # Key: "{platform_session_id}:{source}" - cleared on daemon restart
        self._injected_sessions: set[str] = set()

        # Create all subsystems via factory
        components = HookManagerFactory.create(
            daemon_host=daemon_host,
            daemon_port=daemon_port,
            llm_service=llm_service,
            llm_service_resolver=self._current_llm_service,
            config=config,
            hook_logger=self.logger,
            loop=self._loop,
            broadcaster=broadcaster,
            tool_proxy_getter=tool_proxy_getter,
            message_processor_resolver=message_processor_resolver or (lambda: None),
            agent_runner=agent_runner,
            completion_registry=completion_registry,
            config_runtime=config_runtime,
            database=database,
            session_manager=session_manager,
            get_machine_id=self.get_machine_id,
            resolve_project_id=self._resolve_project_id,
            code_index_trigger=code_index_trigger,
            memory_manager=memory_manager,
            terminal_manager=terminal_manager,
        )

        # Unpack all subsystems from factory components
        self._config = components.config
        self._database = components.database
        self._daemon_client = components.daemon_client
        self._transcript_processor = components.transcript_processor
        self._session_task_manager = components.session_task_manager
        self._memory_storage = components.memory_storage
        self._task_manager = components.task_manager
        self._agent_run_manager = components.agent_run_manager
        self._worktree_manager = components.worktree_manager
        self._stop_registry = components.stop_registry
        self._progress_tracker = components.progress_tracker
        self._stuck_detector = components.stuck_detector
        self._workflow_loader = components.workflow_loader
        self._skill_manager = components.skill_manager
        self._pipeline_executor = components.pipeline_executor
        self._workflow_handler = components.workflow_handler
        self._webhook_dispatcher = components.webhook_dispatcher
        self._session_manager = cast(HookSessionManager, components.session_manager)
        self._project_id_resolver.session_manager = self._session_manager
        self._session_coordinator = components.session_coordinator
        self._session_end_auto_link_worker = components.session_end_auto_link_worker
        self._health_monitor = components.health_monitor
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
                self.logger.warning("Failed to create InterSessionMessageManager: %s", e)
        # Response metadata enrichment service
        from gobby.hooks.event_enrichment import EventEnricher

        self._enricher = EventEnricher(
            session_manager=self._session_manager,
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

    @property
    def session_manager(self) -> "SessionManager":
        """Return the concrete session manager for diagnostics and tests."""
        return cast("SessionManager", self._session_manager)

    def _reregister_active_sessions(self) -> None:
        """Re-register active sessions with the message processor."""
        self._session_coordinator.reregister_active_sessions()

    def _start_health_check_monitoring(self) -> None:
        """Start background daemon health check monitoring."""
        self._health_monitor.start()

    @property
    def event_handlers(self) -> "EventHandlers":
        """Public access to hook event handlers for route and service integrations."""
        return self._event_handlers

    def _get_cached_daemon_status(self) -> tuple[bool, str | None, str, str | None]:
        """Get cached daemon status without making an HTTP call."""
        return self._health_monitor.get_cached_status()

    def _record_machine_ingress(self, event: HookEvent) -> None:
        db = self._database or getattr(self._session_manager, "db", None)
        if db is None:
            return

        data = event.data if isinstance(event.data, dict) else {}
        machine_id = None
        for candidate in (
            event.machine_id,
            _hook_text_field(data, "machine_id", "machineId"),
        ):
            try:
                machine_id = str(UUID(candidate.strip())) if candidate else None
            except (AttributeError, ValueError):
                # Hook payloads are untrusted input; a non-UUID identity is
                # unattributable, never fatal to hook processing.
                self.logger.debug(
                    "Ignoring non-UUID machine id from hook ingress",
                    extra={"machine_id": candidate},
                )
                continue
            if machine_id is not None:
                break
        if machine_id is None:
            return
        try:
            machine = LocalMachineManager(db).refresh_seen(
                machine_id,
                hostname=_hook_text_field(data, "hostname", "host_name", "host"),
                os=_hook_text_field(data, "os", "platform", "operating_system"),
                label=_hook_text_field(data, "machine_label", "machineLabel"),
                tailscale_name=_hook_text_field(data, "tailscale_name", "tailscaleName"),
            )
            if machine is None:
                self.logger.debug(
                    "Ignoring unknown machine id from hook ingress",
                    extra={"machine_id": machine_id},
                )
        except psycopg.Error as exc:
            self.logger.debug(
                "Failed to refresh machine registry from hook ingress",
                extra={"error": str(exc), "machine_id": machine_id},
                exc_info=True,
            )

    def handle(
        self,
        event: HookEvent,
    ) -> HookResponse:
        """Handle a unified HookEvent from any CLI source."""
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

    async def handle_async(
        self,
        event: HookEvent,
    ) -> HookResponse:
        """Async entry point for event-loop hook callers."""
        with create_span(
            "hook.handle",
            attributes={
                "event_type": str(event.event_type),
                "source": str(event.source),
            },
        ) as span:
            try:
                response = await self._handle_internal_async(event)
                if span.is_recording():
                    span.set_attribute("decision", response.decision)
                return response
            except Exception as e:
                if span.is_recording():
                    span.record_exception(e)
                raise

    def _handle_internal(self, event: HookEvent) -> HookResponse:
        """Internal handle logic wrapped by span."""
        daemon_unavailable = ensure_daemon_ready(event, self._health_monitor, self.logger)
        if daemon_unavailable:
            return daemon_unavailable

        return self._handle_after_daemon_ready(event)

    async def _handle_internal_async(self, event: HookEvent) -> HookResponse:
        """Internal async handle logic wrapped by span."""
        daemon_unavailable = await ensure_daemon_ready_async(
            event,
            self._health_monitor,
            self.logger,
        )
        if daemon_unavailable:
            return daemon_unavailable

        return await asyncio.to_thread(self._handle_after_daemon_ready, event)

    def _handle_after_daemon_ready(
        self,
        event: HookEvent,
        blocking_deadline: float | None = None,
    ) -> HookResponse:
        """Run hook handling after the daemon readiness gate has passed."""
        if blocking_deadline is None:
            blocking_deadline = new_blocking_effect_deadline()
        # SESSION_START is special: the handler establishes the canonical
        # platform session first (including pre-created web-chat rows). Doing a
        # generic lookup here can auto-register a stray duplicate before the
        # handler gets a chance to bind the real session.
        if event.event_type == HookEventType.SESSION_START:
            # Caller-supplied _platform_session_id (e.g. the X-Gobby-Session-Id
            # header) must be validated against storage before anything reads
            # it; unknown ids are popped so the handler binds the real session.
            self._session_lookup.validate_platform_session_metadata(event)
            project_resolution = resolve_hook_project_context(
                event,
                session_manager=self._session_manager,
                resolve_project_id=self._resolve_project_id,
                logger=self.logger,
            )
            if project_resolution.skipped:
                self.logger.debug(
                    "Skipping SESSION_START without project context: %s",
                    project_resolution.reason,
                )
                return HookResponse(decision="allow")
        else:
            project_resolution = resolve_hook_project_context(
                event,
                session_manager=self._session_manager,
                resolve_project_id=self._resolve_project_id,
                logger=self.logger,
            )
            if project_resolution.skipped:
                self.logger.debug(
                    "Skipping hook without project context: event=%s reason=%s",
                    event.event_type.value,
                    project_resolution.reason,
                )
                return HookResponse(decision="allow")
            # Resolve platform session_id from CLI external_id. Terminal
            # events defer session mutations (revive/backfill) until the
            # identity fence accepts the hook: a stale old-run SessionEnd
            # must not write dead-process terminal identity onto the
            # durable session.
            gated = event.event_type in TERMINAL_INGRESS_HOOK_TYPES
            platform_session_id = self._session_lookup.resolve(
                event,
                apply_session_mutations=not gated,
            )
            if gated:
                ingress = validate_managed_agent_hook(
                    event,
                    session_manager=self._session_manager,
                    agent_run_manager=self._agent_run_manager,
                    database=self._database,
                    completion_registry=self._completion_registry,
                    registry_loop=self._loop,
                )
                if not ingress.accepted:
                    if ingress.ambiguous:
                        self.logger.warning(
                            "Discarding ambiguous managed terminal hook envelope",
                            extra={
                                "event_type": event.event_type.value,
                                "session_id": platform_session_id,
                                "envelope_id": event.data.get("source_event_id"),
                                "reason": ingress.reason,
                            },
                        )
                    else:
                        self.logger.info(
                            "Ignoring stale managed hook for run %s (event=%s)",
                            ingress.run_id,
                            event.event_type.value,
                        )
                    return HookResponse(decision="allow")
                self._session_lookup.apply_session_mutations(event, platform_session_id)
            self._record_session_activity_pulse(event)

        self._record_machine_ingress(event)

        # Translate #N session references to UUIDs for MCP tool calls.
        # #N is human-friendly but ambiguous across projects (seq_num is per-project).
        # The hook has project context; the MCP server doesn't. Resolve here so
        # downstream tools get unambiguous UUIDs.
        self._resolve_session_refs_in_tool_input(event)

        # Get handler for this event type
        handler = self._get_event_handler(event.event_type)
        if handler is None:
            self.logger.warning("No handler for event type: %s", event.event_type)
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
                    self.logger.exception("Event handler %s failed: %s", event.event_type, e)
                    return HookResponse(decision="allow", reason=f"Handler error: {e}")

            # Blocked or sessionless starts never reach session-scoped work:
            # without a canonical _platform_session_id, rule evaluation would
            # fall back to a session id that has no sessions row.
            if response.decision in ("block", "deny") or not event.metadata.get(
                "_platform_session_id"
            ):
                return self._complete_response(
                    event,
                    response,
                    None,
                    preserve_original=True,
                    suppress_webhooks=True,
                )

            self._record_session_activity_pulse(event)
            reconcile_session_activation(event, self._event_handlers, logger=self.logger)

            with create_span("hook.session_start.rules"):
                workflow_context, blocking_response = self._evaluate_workflow_rules(
                    event, blocking_deadline
                )
                if blocking_response:
                    return self._complete_response(
                        event, blocking_response, workflow_context, preserve_original=True
                    )

            with create_span("hook.session_start.webhooks"):
                webhook_block = self._evaluate_blocking_webhooks(event, blocking_deadline)
                if webhook_block:
                    return self._complete_response(
                        event, webhook_block, workflow_context, preserve_original=True
                    )
        else:
            if event.event_type in (HookEventType.BEFORE_AGENT, HookEventType.BEFORE_TOOL):
                reconcile_session_activation(event, self._event_handlers, logger=self.logger)

            workflow_context, blocking_response = self._evaluate_workflow_rules(
                event, blocking_deadline
            )
            if blocking_response:
                return self._complete_response(
                    event, blocking_response, workflow_context, preserve_original=True
                )
            webhook_block = self._evaluate_blocking_webhooks(event, blocking_deadline)
            if webhook_block:
                return self._complete_response(
                    event, webhook_block, workflow_context, preserve_original=True
                )

            try:
                response = handler(event)
            except Exception as e:
                self.logger.exception("Event handler %s failed: %s", event.event_type, e)
                return HookResponse(decision="allow", reason=f"Handler error: {e}")

        return self._complete_response(event, response, workflow_context)

    def _complete_response(
        self,
        event: HookEvent,
        response: HookResponse,
        workflow_context: str | None,
        *,
        preserve_original: bool = False,
        suppress_webhooks: bool = False,
    ) -> HookResponse:
        """Enrich and notify observers, preserving terminal block responses."""
        observer_response = copy.deepcopy(response) if preserve_original else response
        original_decision = response.decision
        original_reason = response.reason

        # Stringified call_tool arguments may be normalized for rule evaluation.
        # The MCP proxy validates/coerces the actual target arguments, so this
        # should not become a CLI retry/update payload.
        event.data.pop("_input_coerced", None)

        # Propagate rewrite_input from rule evaluation to response (PreToolUse)
        if "_modified_input" in event.metadata:
            observer_response.modified_input = event.metadata.pop("_modified_input")
            observer_response.auto_approve = event.metadata.pop("_auto_approve", False)

        normalized_tool_name = (event.data or {}).get("tool_name")
        if isinstance(normalized_tool_name, str):
            observer_response.metadata.setdefault("_normalized_tool_name", normalized_tool_name)

        with create_span("hook.enrich"):
            try:
                self._enricher.enrich(event, observer_response, workflow_context=workflow_context)
            except Exception as e:
                self.logger.exception("Response enrichment failed: %s", e)

        try:
            from gobby.workflows.engine.delivery_formatting import (
                finalize_staged_memory_delivery,
            )

            finalize_staged_memory_delivery(
                event,
                response if preserve_original else observer_response,
                database=self._database,
                logger=self.logger,
            )
        except Exception as e:
            self.logger.exception("Staged memory delivery failed: %s", e)

        if preserve_original:
            observer_response.decision = original_decision
            observer_response.reason = original_reason

        schedule_hook_broadcast(self.broadcaster, event, observer_response, self._loop, self.logger)

        # Dispatch non-blocking webhooks (fire-and-forget)
        if not suppress_webhooks:
            try:
                self._dispatch_webhooks_async(event, observer_response)
            except Exception as e:
                self.logger.warning("Non-blocking webhook dispatch failed: %s", e)

        return response if preserve_original else observer_response

    def _get_event_handler(self, event_type: HookEventType) -> Any | None:
        """Get the handler method for a HookEventType."""
        return self._event_handlers.get_handler(event_type)

    @staticmethod
    def _record_session_activity_pulse(event: HookEvent) -> None:
        """Record a non-statusline activity pulse for the event's platform session."""
        platform_id = event.metadata.get("_platform_session_id")
        if isinstance(platform_id, str) and platform_id:
            record_session_activity(platform_id)

    def _resolve_session_refs_in_tool_input(self, event: HookEvent) -> None:
        """Resolve #N session references to UUIDs in MCP tool arguments."""
        resolve_session_refs_in_tool_input(event, self._session_manager)

    def _try_resolve_session_field(
        self, d: dict[str, Any], field: str, project_id: str | None
    ) -> bool:
        """Resolve a #N session reference in d[field] to UUID in place."""
        return try_resolve_session_field(
            d,
            field,
            session_manager=self._session_manager,
            project_id=project_id,
        )

    @staticmethod
    def _summarize_mcp_calls(mcp_calls: list[dict[str, Any]]) -> list[str]:
        """Return compact server/tool labels for workflow-triggered MCP calls."""
        return WorkflowRuleEvaluator._summarize_mcp_calls(mcp_calls)

    def _log_workflow_evaluation(
        self,
        event: HookEvent,
        workflow_response: HookResponse,
        mcp_calls: list[dict[str, Any]],
    ) -> None:
        """Log workflow decisions, keeping routine allow decisions at debug level."""
        self._create_rule_evaluator()._log_workflow_evaluation(
            event,
            workflow_response,
            mcp_calls,
        )

    def evaluate_workflow_rules(self, event: HookEvent) -> tuple[str | None, HookResponse | None]:
        """Evaluate workflow rules and dispatch mcp_call effects."""
        return self._evaluate_workflow_rules(event)

    def _evaluate_workflow_rules(
        self,
        event: HookEvent,
        blocking_deadline: float | None = None,
    ) -> tuple[str | None, HookResponse | None]:
        """Evaluate workflow rules and dispatch mcp_call effects."""
        return self._create_rule_evaluator(blocking_deadline).evaluate(event)

    def _create_rule_evaluator(
        self,
        blocking_deadline: float | None = None,
    ) -> WorkflowRuleEvaluator:
        """Create a rule evaluator bound to the manager's current dependencies."""

        def dispatch_mcp_calls(
            calls: list[dict[str, Any]],
            event: HookEvent,
        ) -> list[dict[str, Any]]:
            if blocking_deadline is None:
                return self._dispatch_mcp_calls(calls, event)
            return self._dispatch_mcp_calls(calls, event, deadline=blocking_deadline)

        return WorkflowRuleEvaluator(
            workflow_handler=self._workflow_handler,
            dispatch_mcp_calls=dispatch_mcp_calls,
            format_discovery_result=self._format_discovery_result,
            database=self._database,
            logger=self.logger,
            blocking_deadline=blocking_deadline,
        )

    def _evaluate_blocking_webhooks(
        self,
        event: HookEvent,
        blocking_deadline: float | None = None,
    ) -> HookResponse | None:
        """Evaluate blocking webhooks before handler execution."""
        return webhook_dispatcher.evaluate_blocking_webhooks(
            event,
            self._webhook_dispatcher,
            self.logger,
            self._loop,
            deadline=blocking_deadline,
        )

    def _dispatch_webhooks_sync(self, event: HookEvent, blocking_only: bool = False) -> list[Any]:
        """Dispatch webhooks synchronously (for blocking webhooks)."""
        return webhook_dispatcher.dispatch_webhooks_sync(
            event, self._webhook_dispatcher, self.logger, blocking_only
        )

    def _dispatch_webhooks_async(
        self, event: HookEvent, response: HookResponse | None = None
    ) -> None:
        """Dispatch non-blocking webhooks asynchronously (fire-and-forget)."""
        webhook_dispatcher.dispatch_webhooks_async(
            event, self._webhook_dispatcher, self.logger, self._loop, response
        )

    def _dispatch_mcp_calls(
        self,
        mcp_calls: list[dict[str, Any]],
        event: HookEvent,
        *,
        deadline: float | None = None,
    ) -> list[dict[str, Any]]:
        """Dispatch mcp_call effects from rule engine evaluation."""
        return mcp_dispatcher.dispatch_mcp_calls(
            mcp_calls,
            event,
            self.tool_proxy_getter,
            self._loop,
            self.logger,
            deadline=deadline,
        )

    def _run_coro_blocking(
        self,
        coro: Any,
        *,
        label: str | None = None,
        timeout_seconds: float = 30,
    ) -> Any:
        """Run a coroutine blocking, using the best available event loop strategy."""
        return mcp_dispatcher.run_coro_blocking(
            coro,
            self._loop,
            self.logger,
            label=label,
            timeout_seconds=timeout_seconds,
        )

    async def _proxy_self_call(self, proxy: Any, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Route _proxy/* tool calls to ToolProxyService methods directly."""
        return await mcp_dispatcher.proxy_self_call(proxy, tool, args)

    @staticmethod
    def _format_discovery_result(dr: dict[str, Any]) -> str:
        """Format a proxy discovery result for context injection."""
        return mcp_dispatcher.format_discovery_result(dr)

    def _dedup_memory_results(self, result: dict[str, Any], session_id: str) -> dict[str, Any]:
        """Filter already-injected memories and track newly-injected IDs."""
        return self._create_rule_evaluator().dedup_memory_results(result, session_id)

    def _dedup_skill_results(self, result: dict[str, Any], session_id: str) -> dict[str, Any]:
        """Filter already-suggested skills and low-relevance results."""
        return self._create_rule_evaluator().dedup_skill_results(result, session_id)

    def _current_llm_service(self) -> Any | None:
        """Resolve the current runtime epoch's LLM service, per event."""
        capture = getattr(self._config_runtime, "capture", None)
        if callable(capture):
            try:
                bundle = capture()
            except Exception:
                self.logger.debug("Hook LLM resolution fell back to creation-time service")
            else:
                resolved = getattr(bundle.services.get("ai_services"), "llm_service", None)
                if resolved is not None:
                    return resolved
        return self._llm_service

    def _dispatch_session_summaries(
        self,
        session_id: str,
        background: bool = False,
        done_event: threading.Event | None = None,
        set_handoff_ready: bool = False,
    ) -> None:
        """Fire session summary generation."""
        dispatcher = SessionSummaryDispatcher(
            session_manager=self._session_manager,
            llm_service=self._current_llm_service(),
            session_summary_config=getattr(self._config, "session_summary", None),
            database=self._database,
            loop=self._loop,
            logger=self.logger,
        )
        dispatcher.dispatch(
            session_id,
            _background=background,
            done_event=done_event,
            set_handoff_ready=set_handoff_ready,
        )

    def shutdown(self) -> None:
        """
        Clean up HookManager resources on daemon shutdown.

        Stops background health check monitoring and transcript watchers.
        Closes only database handles created by this HookManager.
        """
        if self._shutdown_complete:
            self.logger.debug("HookManager shutdown already complete")
            return

        self.logger.debug("HookManager shutting down")

        # Stop health check monitoring (delegated to HealthMonitor)
        self._health_monitor.stop()

        self._session_end_auto_link_worker.shutdown()
        self._close_webhook_dispatcher_sync()
        self._workflow_handler.shutdown()

        if self._owns_database and hasattr(self, "_database"):
            self._database.close()

        self._shutdown_complete = True
        self.logger.debug("HookManager shutdown complete")

    async def shutdown_async(self) -> None:
        """Clean up HookManager resources from an async shutdown context."""
        if self._shutdown_complete:
            self.logger.debug("HookManager shutdown already complete")
            return

        self.logger.debug("HookManager shutting down")

        # Stop health check monitoring (delegated to HealthMonitor)
        self._health_monitor.stop()

        await asyncio.to_thread(self._session_end_auto_link_worker.shutdown)
        await self._close_webhook_dispatcher_async()
        self._workflow_handler.shutdown()

        if self._owns_database and hasattr(self, "_database"):
            self._database.close()

        self._shutdown_complete = True
        self.logger.debug("HookManager shutdown complete")

    async def _close_webhook_dispatcher_async(self) -> None:
        try:
            await self._webhook_dispatcher.close()
        except Exception as exc:
            self._log_webhook_dispatcher_close_failure(exc)

    def _close_webhook_dispatcher_sync(self) -> None:
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is not None:
            running_loop.create_task(self._close_webhook_dispatcher_async())
            self.logger.debug("Scheduled webhook dispatcher close on current event loop")
            return

        try:
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._close_webhook_dispatcher_async(), self._loop
                ).result(timeout=5.0)
            else:
                asyncio.run(self._close_webhook_dispatcher_async())
        except concurrent.futures.TimeoutError:
            self.logger.warning(
                "Timed out closing webhook dispatcher after 5.0s",
                exc_info=True,
            )
        except Exception as exc:
            self._log_webhook_dispatcher_close_failure(exc)

    def _log_webhook_dispatcher_close_failure(self, exc: Exception) -> None:
        message = str(exc) or "<no message>"
        self.logger.warning(
            "Failed to close webhook dispatcher (%s): %s",
            type(exc).__name__,
            message,
            exc_info=True,
        )

    # ==================== HELPER METHODS ====================

    def get_machine_id(self) -> str | None:
        """Get unique machine identifier."""
        from gobby.utils.machine_id import get_machine_id as _get_machine_id

        return _get_machine_id()

    def _resolve_project_id(self, project_id: str | None, cwd: str | None) -> str:
        """Resolve project_id from explicit input, cwd, or personal fallback."""
        return self._project_id_resolver.resolve(project_id, cwd)

    def _ensure_project_in_db(self, project_context: dict[str, Any]) -> None:
        """Ensure project from project.json exists in the database."""
        self._project_id_resolver.session_manager = getattr(self, "_session_manager", None)
        self._project_id_resolver.logger = self.logger
        self._project_id_resolver.ensure_project_in_db(project_context)
