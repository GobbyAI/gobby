"""HTTP, WebSocket, and broadcast setup for GobbyRunner."""

from __future__ import annotations

import logging
import weakref
from typing import TYPE_CHECKING

from gobby.ai.vision import build_daemon_vision_extract_service
from gobby.app_context import ServiceContainer, set_app_context
from gobby.config.app import deep_merge
from gobby.providers.capabilities.coverage import ModelMetadataCoverageAuditor
from gobby.providers.capabilities.refresh import CapabilityRefreshCoordinator
from gobby.providers.capabilities.resolve import CapabilityResolver
from gobby.providers.capabilities.store import ProviderCapabilityStore
from gobby.servers.http import HTTPServer
from gobby.servers.provider_model_discovery import (
    claude_uses_loopback_model_endpoint,
    codex_uses_loopback_model_endpoint,
    load_claude_settings,
    load_codex_config,
    load_qwen_settings,
    qwen_local_model_values,
)
from gobby.servers.websocket.chat.runtime_manager import WebChatRuntimeManager
from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry
from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.server import WebSocketServer
from gobby.storage.model_metadata import ModelMetadataStore

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger(__name__)


def _local_model_metadata_exclusions() -> frozenset[tuple[str, str]]:
    settings = load_qwen_settings(deep_merge=deep_merge, logger=logger)
    return frozenset(("qwen", model) for model in qwen_local_model_values(settings))


def _local_provider_metadata_exclusions() -> frozenset[str]:
    providers: set[str] = set()
    if codex_uses_loopback_model_endpoint(load_codex_config(logger=logger)):
        providers.add("codex")
    claude_settings = load_claude_settings(deep_merge=deep_merge, logger=logger)
    if claude_uses_loopback_model_endpoint(claude_settings):
        providers.add("claude")
    return frozenset(providers)


def register_config_event_publisher(runner: GobbyRunner) -> None:
    """Publish each reconciled configuration revision to WebSocket clients."""
    websocket_server = runner.websocket_server
    if websocket_server is None:
        return
    runner.config_runtime.register_revision_publisher(websocket_server.broadcast_config_event)


def _resolve_message_processor(runner: GobbyRunner) -> object | None:
    runtime = runner.config_runtime
    if not runtime.ready:
        return runner.message_processor
    bundle = runtime.capture()
    if any(
        failure.subscriber == "message_processor"
        for failure in bundle.snapshot.failed_live_keys.values()
    ):
        return None
    return bundle.services.get("message_processor")


def init_servers(runner: GobbyRunner) -> None:
    """Initialize HTTP server, WebSocket server, and broadcasting."""
    config = runner.startup_config
    web_chat_session_registry = WebChatSessionRegistry()
    runner.wake_dispatcher.set_web_chat_session_registry(web_chat_session_registry)
    http_server_ref: weakref.ReferenceType[HTTPServer] | None = None
    provider_capability_store = ProviderCapabilityStore(runner.database)
    model_metadata_store = ModelMetadataStore(runner.database)
    model_metadata_coverage_auditor = ModelMetadataCoverageAuditor(
        provider_capability_store,
        model_metadata_store,
        config.ai.model_metadata_aliases,
        run_db=getattr(runner.db_executor, "run", None),
        excluded_models=_local_model_metadata_exclusions,
        excluded_providers=_local_provider_metadata_exclusions,
    )
    provider_capability_service = CapabilityRefreshCoordinator(
        provider_capability_store,
        run_db=getattr(runner.db_executor, "run", None),
        coverage_auditor=model_metadata_coverage_auditor,
    )
    provider_capability_service.prepare()
    provider_capability_resolver = CapabilityResolver(
        provider_capability_service,
        model_metadata_store,
        config.ai.model_metadata_aliases,
    )

    def tool_proxy_getter() -> object | None:
        http_server = http_server_ref() if http_server_ref is not None else None
        return http_server.tool_proxy if http_server is not None else None

    services = ServiceContainer(
        database=runner.database,
        db_executor=runner.db_executor,
        worktree_delete_executor=runner.worktree_delete_executor,
        coverage_executor=runner.coverage_executor,
        database_concurrency=runner.database_concurrency,
        database_watchdog=runner.database_watchdog,
        session_manager=runner.session_manager,
        task_manager=runner.task_manager,
        span_storage=runner.span_storage,
        memory_backup_manager=runner.memory_backup_manager,
        memory_manager=runner.memory_manager,
        memory_dream_coordinator=runner.memory_dream_coordinator,
        text_generation_service=runner.text_generation_service,
        tool_chat_service=runner.tool_chat_service,
        llm_service=runner.llm_service,
        vector_store=runner.vector_store,
        mcp_manager=runner.mcp_proxy,
        mcp_db_manager=runner.mcp_db_manager,
        metrics_manager=runner.metrics_manager,
        agent_runner=runner.agent_runner,
        message_processor_resolver=lambda: _resolve_message_processor(runner),
        task_validator=runner.task_validator,
        worktree_storage=runner.worktree_storage,
        clone_storage=runner.clone_storage,
        git_manager=runner.git_manager,
        project_id=runner.project_id,
        pipeline_executor=runner.pipeline_executor,
        workflow_loader=runner.workflow_loader,
        pipeline_execution_manager=runner.pipeline_execution_manager,
        completion_registry=runner.completion_registry,
        wake_dispatcher=runner.wake_dispatcher,
        agent_lifecycle_monitor=runner.agent_lifecycle_monitor,
        attention_manager=runner.attention_manager,
        attention_metadata_store=getattr(runner, "attention_metadata_store", None),
        detection_registry=runner.detection_registry,
        communications_manager=runner.communications_manager,
        code_indexer=runner.code_indexer,
        code_index_pruner=getattr(runner, "code_index_pruner", None),
        cron_storage=runner.cron_storage,
        cron_scheduler=runner.cron_scheduler,
        system_automation_loop=runner.system_automation_loop,
        skill_manager=runner.skill_manager,
        hub_manager=runner.hub_manager,
        config_runtime=runner.config_runtime,
        provider_capability_service=provider_capability_service,
        provider_capability_resolver=provider_capability_resolver,
        model_metadata_coverage_auditor=model_metadata_coverage_auditor,
        web_chat_runtime_manager=None,
        web_chat_session_registry=web_chat_session_registry,
        prompt_manager=runner.prompt_manager,
        dev_mode=runner._dev_mode,
        tool_proxy_getter=tool_proxy_getter,
    )

    set_app_context(services)
    if runner.cron_scheduler and getattr(runner.cron_scheduler, "executor", None):
        runner.cron_scheduler.executor.services = services
    if runner.system_automation_loop:
        runner.system_automation_loop.set_services(services)

    if runner.communications_manager:
        from gobby.communications.reactions import ReactionHandler

        runner.communications_manager.reaction_handler = ReactionHandler(
            runner.communications_manager.store, services
        )
        runner.communications_manager.set_vision_extract_service(
            build_daemon_vision_extract_service(config)
        )

    codex_client = None
    from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter

    if CodexAdapter.is_codex_available():
        from gobby.adapters.codex_impl.client import CodexAppServerClient

        codex_client = CodexAppServerClient()
        logger.info("Codex app-server client created (will start after HTTP readiness)")
    runner.codex_client = codex_client

    services.web_chat_runtime_manager = WebChatRuntimeManager(
        codex_client=codex_client,
        daemon_config=config,
        config_resolver=lambda: (
            runner.config_runtime.snapshot.active if runner.config_runtime.ready else None
        ),
    )

    runner.http_server = HTTPServer(
        services=services,
        startup_config=config,
        port=runner.bootstrap_config.daemon_port,
        test_mode=config.test_mode,
        codex_client=codex_client,
        bootstrap_config=runner.bootstrap_config,
    )
    _bind_runtime_grants(runner.http_server, runner)
    http_server_ref = weakref.ref(runner.http_server)
    runner.http_server.set_runner_getter(weakref.ref(runner))

    if runner.communications_manager and runner.http_server.transcript_reader:
        from gobby.communications.native_plan_actions import NativePlanActionService
        from gobby.communications.session_notifications import SessionNotificationService
        from gobby.communications.telegram_actions import TelegramActionController
        from gobby.sessions.mailbox import MailboxService
        from gobby.storage.inter_session_messages import InterSessionMessageManager

        mailbox = MailboxService(
            db=runner.database,
            message_manager=InterSessionMessageManager(runner.database),
            session_manager=runner.session_manager,
            wake_dispatcher=runner.wake_dispatcher,
        )
        native_plan_actions = NativePlanActionService(
            runner.session_manager,
            runner.detection_registry,
        )
        runner.communications_manager.set_session_notification_service(
            SessionNotificationService(
                runner.communications_manager,
                runner.session_manager,
                runner.http_server.transcript_reader,
                native_plan_actions=native_plan_actions,
            )
        )
        runner.communications_manager.set_telegram_action_controller(
            TelegramActionController(
                runner.communications_manager,
                runner.session_manager,
                mailbox,
                native_plan_actions,
            )
        )

    if runner.pipeline_executor is not None:
        runner.pipeline_executor.tool_proxy_getter = tool_proxy_getter

    runner.websocket_server = None
    if config.websocket.enabled:
        websocket_config = WebSocketConfig(
            host=runner.bootstrap_config.bind_host,
            port=runner.bootstrap_config.websocket_port,
            ping_interval=config.websocket.ping_interval,
            ping_timeout=config.websocket.ping_timeout,
        )
        runner.websocket_server = WebSocketServer(
            config=websocket_config,
            mcp_manager=runner.mcp_proxy,
            auth_callback=runner.http_server.auth_service.verify_ws_token,
            session_manager=runner.session_manager,
            db_executor=runner.db_executor,
            daemon_config=config,
            bootstrap_config=runner.bootstrap_config,
            config_runtime=runner.config_runtime,
            internal_manager=runner.http_server._internal_manager,
            web_chat_session_registry=web_chat_session_registry,
            tool_proxy_getter=tool_proxy_getter,
            completion_registry=runner.completion_registry,
        )
        runner.websocket_server.web_chat_runtime_manager = services.web_chat_runtime_manager
        attention_manager = services.attention_manager
        if attention_manager is not None:
            runner.websocket_server.configure_attention_ordering(attention_manager.ordering)
        attention_metadata_store = services.attention_metadata_store
        if attention_metadata_store is not None:
            runner.websocket_server.configure_attention_metadata(attention_metadata_store)
        runner.http_server.websocket_server = runner.websocket_server
        runner.http_server.services.websocket_server = runner.websocket_server
        runner.http_server.broadcaster.websocket_server = runner.websocket_server

        if runner.communications_manager:
            from gobby.communications.chat_backend import ChatSessionCommsBackend

            runner.communications_manager.set_websocket_broadcast(runner.websocket_server.broadcast)
            runner.communications_manager.set_voice_transcriber_getter(
                runner.websocket_server.get_voice_transcriber,
                timeout_seconds=config.voice.transcription_timeout_seconds,
            )
            runner.communications_manager.responder.set_backend(
                ChatSessionCommsBackend(
                    runner.websocket_server,
                    runner.communications_manager,
                    tts_provider_getter=runner.websocket_server.get_tts_provider,
                )
            )

        if runner.message_processor:
            runner.message_processor.websocket_server = runner.websocket_server
            runner.message_processor.session_manager = runner.session_manager

        from gobby.runner_broadcasting import (
            setup_agent_event_broadcasting,
            setup_pipeline_event_broadcasting,
        )

        setup_agent_event_broadcasting(runner.websocket_server)

        if runner.pipeline_executor:
            setup_pipeline_event_broadcasting(runner.websocket_server, runner.pipeline_executor)

    register_config_event_publisher(runner)

    if runner.cron_scheduler and (runner.websocket_server or runner.communications_manager):
        from gobby.runner_broadcasting import setup_cron_event_broadcasting

        setup_cron_event_broadcasting(
            runner.websocket_server,
            runner.cron_scheduler,
            runner.communications_manager,
        )

    if runner.communications_manager:
        from gobby.runner_broadcasting import (
            setup_communications_event_broadcasting,
            setup_session_status_communications,
        )

        setup_communications_event_broadcasting(
            runner.websocket_server,
            runner.communications_manager,
        )
        setup_session_status_communications(
            runner.session_manager,
            runner.communications_manager,
            lambda: runner.main_loop,
        )


def _bind_runtime_grants(server: HTTPServer, runner: GobbyRunner) -> None:
    import time
    from datetime import UTC, datetime, timedelta
    from uuid import UUID

    from gobby.runtime_grants.handshake import HandshakeRejection, HandshakeService
    from gobby.runtime_grants.schema import GrantPrincipal, PostgresDirect
    from gobby.runtime_grants.service import DeploymentGrantContext, GrantService
    from gobby.servers.grant_auth import LiveLeaseGrantService
    from gobby.servers.lease_fence import EffectFence, bind_fenced_writer

    lease = getattr(runner, "daemon_lease", None)
    runtime = getattr(runner, "config_runtime", None)
    if lease is None or runtime is None:
        return
    presenter = LiveLeaseGrantService(runtime, lease, clock=lambda: int(time.time()))
    fence = EffectFence()
    server.effect_fence = fence
    database = getattr(runner, "database", None)
    if database is not None:
        bind_fenced_writer(database, lease)
    server.grant_service = presenter
    server.auth_service.bind_runtime(
        grant_service=presenter,
        lease_live=lease.owns_live_lease,
        local_machine_id=lease.machine_id,
        effect_fence=fence,
        clock=lambda: int(time.time()),
    )

    credentials = getattr(runner, "managed_credential_manager", None)
    secrets = getattr(runner, "secret_store", None)
    if credentials is None or secrets is None or database is None:
        return

    def _project_admitted(project_id: str) -> bool:
        try:
            row = database.fetchone("SELECT 1 FROM projects WHERE id = %s", (project_id,))
        except Exception:
            return False
        return row is not None

    def _issue_postgres(principal: GrantPrincipal) -> PostgresDirect:
        session_id = principal.session_id or str(principal.project_id)
        try:
            issued = credentials.issue_interactive(
                deployment_token=str(lease.deployment_token),
                project_id=UUID(principal.project_id),
                session_id=UUID(session_id),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                secret_store=secrets,
            )
        except Exception as error:
            logger.exception("interactive grant credential issuance failed")
            raise HandshakeRejection(
                f"interactive credential issuance failed: {error}",
                code="managed_source",
            ) from error
        return PostgresDirect(
            dsn=issued.dsn,
            role_name=issued.role_name,
            credential_generation=issued.credential_generation,
            valid_until=int(issued.expires_at.timestamp()),
        )

    def _handshake_factory() -> HandshakeService:
        token = getattr(lease, "deployment_token", None)
        epoch = getattr(lease, "fencing_epoch", None)
        secret = getattr(lease, "grant_signing_secret", None)
        if token is None or epoch is None or not secret:
            raise RuntimeError("active-daemon lease has no grant signing context")
        grants = GrantService(
            runtime=runtime,
            context=DeploymentGrantContext(
                token=str(token),
                fencing_epoch=int(epoch),
                signing_secret=str(secret),
            ),
            clock=lambda: int(time.time()),
        )
        operator_token = server.auth_service.local_token() or ""
        return HandshakeService(
            grants=grants,
            local_machine_id=str(lease.machine_id),
            operator_token=operator_token,
            issue_postgres=_issue_postgres,
            admitted_projects=_project_admitted,
            clock=lambda: int(time.time()),
        )

    server.handshake_factory = _handshake_factory
    try:
        server.handshake_service = _handshake_factory()
    except Exception:
        logger.exception("runtime handshake factory is not ready")
