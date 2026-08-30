"""HTTP, WebSocket, and broadcast setup for GobbyRunner."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import weakref
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg

from gobby.ai.vision import build_daemon_vision_extract_service
from gobby.app_context import ServiceContainer, set_app_context
from gobby.config.app import deep_merge
from gobby.providers.capabilities.coverage import ModelMetadataCoverageAuditor
from gobby.providers.capabilities.refresh import CapabilityRefreshCoordinator
from gobby.providers.capabilities.resolve import CapabilityResolver
from gobby.providers.capabilities.store import ProviderCapabilityStore
from gobby.servers.generation_endpoint_health import GenerationEndpointHealthCoordinator
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
    from gobby.runtime_grants.schema import GrantPrincipal, PostgresDirect
    from gobby.storage.managed_credential_types import SecretStore
    from gobby.storage.managed_credentials import ManagedCredentialManager
    from gobby.storage.terminals import AttachLocator
    from gobby.terminals.frame_client import FrameClient

logger = logging.getLogger(__name__)

_CREDENTIAL_ISSUANCE_FAILED = "credential issuance failed"


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
    """Notify daemon consumers after each reconciled configuration revision."""
    websocket_server = runner.websocket_server
    endpoint_health = runner.http_server.services.generation_endpoint_health

    async def publish(revision: int) -> None:
        if endpoint_health is not None:
            endpoint_health.configuration_changed()
        if websocket_server is not None:
            await websocket_server.broadcast_config_event(revision)

    runner.config_runtime.register_revision_publisher(publish)


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
    generation_endpoint_health = GenerationEndpointHealthCoordinator(
        lambda: runner.config_runtime.capture().snapshot.active.ai.generation.endpoints
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
        managed_credential_manager=runner.managed_credential_manager,
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
        terminal_manager=getattr(runner, "terminal_manager", None),
        terminal_runtime_registry=getattr(runner, "terminal_runtime_registry", None),
        terminal_config=getattr(runner, "terminal_config", None),
        terminal_services=getattr(runner, "terminal_services", None),
        terminal_host_config=getattr(runner, "terminal_host_config", None),
        terminal_host_manager=getattr(runner, "terminal_host_manager", None),
        frame_client=getattr(runner, "frame_client", None),
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
        generation_endpoint_health=generation_endpoint_health,
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
    web_chat_codex_factory = None
    from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter

    if CodexAdapter.is_codex_available():
        from gobby.adapters.codex_impl.client import CodexAppServerClient

        codex_client = CodexAppServerClient()

        def _web_chat_codex_factory(**kwargs: Any) -> CodexAppServerClient:
            return CodexAppServerClient(**kwargs)

        web_chat_codex_factory = _web_chat_codex_factory
        logger.info("Codex app-server client created (will start after HTTP readiness)")
    runner.codex_client = codex_client

    services.web_chat_runtime_manager = WebChatRuntimeManager(
        codex_client_factory=web_chat_codex_factory,
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
            terminal_manager=getattr(runner, "terminal_manager", None),
            terminal_runtime_registry=getattr(runner, "terminal_runtime_registry", None),
            write_coordinator=getattr(runner, "write_coordinator", None),
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
        if services.terminal_manager is not None and services.terminal_runtime_registry is not None:
            runner.websocket_server.configure_terminals(
                services.terminal_manager,
                services.terminal_runtime_registry,
                services.terminal_config,
                terminal_services=services.terminal_services,
            )
            _bind_proxy_frame_opener(runner)
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


def issue_grant_postgres(
    principal: GrantPrincipal,
    *,
    credentials: ManagedCredentialManager,
    deployment_token: str,
    secrets: SecretStore,
    managed_bootstrap_dsn: Callable[[object], str],
) -> PostgresDirect:
    """Issue a Postgres capability for a handshake principal.

    Interactive grants accept a missing ``principal.session_id`` (the binding
    column is audit-only); issuance exceptions become a generic handshake
    rejection so credential details stay out of the client envelope.
    """
    from gobby.runtime_grants.handshake import HandshakeRejection
    from gobby.runtime_grants.schema import PostgresDirect
    from gobby.storage.managed_credential_types import CredentialAuthorizationError

    expires_at = datetime.now(UTC) + timedelta(hours=1)
    try:
        if principal.kind == "interactive":
            overlay = principal.code_overlay_project_id
            issued = credentials.issue_interactive(
                deployment_token=deployment_token,
                project_id=UUID(principal.project_id),
                session_id=UUID(principal.session_id) if principal.session_id else None,
                expires_at=expires_at,
                secret_store=secrets,
                code_overlay_project_id=UUID(overlay) if overlay is not None else None,
            )
            return PostgresDirect(
                dsn=issued.dsn,
                role_name=issued.role_name,
                credential_generation=issued.credential_generation,
                valid_until=int(issued.expires_at.timestamp()),
            )
        if principal.kind == "maintenance":
            if principal.execution_id is None:
                raise HandshakeRejection(
                    "maintenance grant requires execution_id",
                    code="claims_mismatch",
                )
            overlay = principal.code_overlay_project_id
            maintenance = credentials.issue_maintenance(
                managed_execution_id=UUID(principal.execution_id),
                project_id=UUID(principal.project_id),
                expires_at=expires_at,
                code_overlay_project_id=UUID(overlay) if overlay is not None else None,
            )
            return PostgresDirect(
                dsn=maintenance.dsn,
                role_name=maintenance.credential.role_name,
                credential_generation=maintenance.credential.credential_generation,
                valid_until=int(maintenance.credential.expires_at.timestamp()),
            )
        if principal.execution_id is None or principal.session_id is None:
            raise HandshakeRejection(
                "managed grant requires execution and session identity",
                code="claims_mismatch",
            )
        managed = credentials.issue(
            managed_execution_id=UUID(principal.execution_id),
            owner_kind="tool_chat" if principal.kind == "tool_chat" else "agent_run",
            session_id=UUID(principal.session_id),
            agent_run_id=(UUID(principal.execution_id) if principal.kind == "agent_run" else None),
            expires_at=expires_at,
        )
        return PostgresDirect(
            dsn=managed_bootstrap_dsn(managed.bootstrap_path),
            role_name=managed.role_name,
            credential_generation=managed.credential_generation,
            valid_until=int(managed.expires_at.timestamp()),
        )
    except HandshakeRejection:
        raise
    except CredentialAuthorizationError as error:
        raise HandshakeRejection(str(error), code="claims_mismatch") from None
    except Exception:
        logger.exception("grant credential issuance failed", extra={"kind": principal.kind})
        raise HandshakeRejection(
            _CREDENTIAL_ISSUANCE_FAILED,
            code="credential_issuance_failed",
        ) from None


def _bind_runtime_grants(server: HTTPServer, runner: GobbyRunner) -> None:
    # Deferred imports stay here: they close a runner <-> runtime_grants cycle.
    from gobby.runtime_grants.handshake import HandshakeRejection, HandshakeService
    from gobby.runtime_grants.revocation import GrantRevocationStore
    from gobby.runtime_grants.schema import GrantBundle, PostgresDirect
    from gobby.runtime_grants.service import DeploymentGrantContext, GrantService
    from gobby.servers.grant_auth import LiveLeaseGrantService
    from gobby.servers.lease_fence import EffectFence, bind_fenced_writer

    lease = getattr(runner, "daemon_lease", None)
    runtime = getattr(runner, "config_runtime", None)
    if lease is None or runtime is None:
        return
    revocations = GrantRevocationStore()
    presenter = LiveLeaseGrantService(
        runtime, lease, clock=lambda: int(time.time()), revocations=revocations
    )
    fence = EffectFence()
    if os.environ.get("GOBBY_TEST_PROTECT") == "1":
        from gobby.servers.admit_barrier import await_test_admit_barrier

        fence.admit_hook = await_test_admit_barrier
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
    bind = getattr(credentials, "bind_grant_revocations", None)
    if callable(bind):
        bind(revocations)

    def _principal_revoked(grant: GrantBundle) -> bool:
        generation = getattr(grant.capabilities.postgres, "credential_generation", None)
        if not isinstance(generation, int) or grant.principal.kind != "interactive":
            return False
        return bool(
            credentials.interactive_generation_revoked(
                deployment_token=grant.deployment.token,
                project_id=UUID(grant.principal.project_id),
                generation=generation,
            )
        )

    revocations.set_principal_revoked(_principal_revoked)

    def _project_admitted(project_id: str) -> bool:
        try:
            row = database.fetchone(
                "SELECT 1 FROM projects WHERE id = %s AND deleted_at IS NULL",
                (project_id,),
            )
        except psycopg.Error as exc:
            logger.warning(
                "project admission query failed",
                extra={"project_id": project_id, "error": str(exc)},
            )
            return False
        return row is not None

    def _indexed_project_admitted(project_id: str) -> bool:
        # Maintenance targets: any shared code-index identity, registered or not,
        # so orphaned path-derived projects can be granted for projection purge.
        try:
            row = database.fetchone(
                "SELECT 1 FROM code_indexed_projects WHERE id = %s",
                (project_id,),
            )
        except psycopg.Error as exc:
            logger.warning(
                "indexed project admission query failed",
                extra={"project_id": project_id, "error": str(exc)},
            )
            return False
        return row is not None

    def _managed_bootstrap_dsn(bootstrap_path: object) -> str:
        payload = json.loads(Path(str(bootstrap_path)).read_text())
        return str(payload["database_url"])

    def _issue_postgres(principal: GrantPrincipal) -> PostgresDirect:
        issued = issue_grant_postgres(
            principal,
            credentials=credentials,
            deployment_token=str(lease.deployment_token),
            secrets=secrets,
            managed_bootstrap_dsn=_managed_bootstrap_dsn,
        )
        if not isinstance(issued, PostgresDirect):
            raise HandshakeRejection(_CREDENTIAL_ISSUANCE_FAILED, code="credential_issuance_failed")
        return issued

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
            revocations=revocations,
        )
        operator_token = server.auth_service.local_token() or ""
        return HandshakeService(
            grants=grants,
            local_machine_id=str(lease.machine_id),
            operator_token=operator_token,
            issue_postgres=_issue_postgres,
            admitted_projects=_project_admitted,
            admitted_maintenance_targets=_indexed_project_admitted,
            clock=lambda: int(time.time()),
        )

    server.handshake_factory = _handshake_factory
    try:
        server.handshake_service = _handshake_factory()
        indexer = getattr(runner, "code_indexer", None)
        if indexer is not None:
            from gobby.runtime_grants.maintenance import HandshakeMaintenanceLaunchFactory

            indexer.launch_factory = HandshakeMaintenanceLaunchFactory(
                handshake=server.handshake_service,
                credentials=credentials,
                operator_token=server.auth_service.local_token() or "",
                machine_id=str(lease.machine_id),
            )
    except Exception:
        logger.exception("runtime handshake factory is not ready")


def _bind_proxy_frame_opener(runner: GobbyRunner) -> None:
    """Connect browser proxy attachments to the live gterm frames socket."""
    server = runner.websocket_server
    if server is None:
        return

    async def open_proxy_frame(locator: AttachLocator) -> FrameClient:
        from gobby.terminals.frame_client import FrameClient
        from gobby.terminals.host_client import HostUnavailableError
        from gobby.terminals.host_protocol import frames_socket_path

        host = getattr(runner, "terminal_host_manager", None)
        if host is None:
            raise HostUnavailableError("gterm host unavailable")
        path = locator.host_socket or str(frames_socket_path(host.socket_dir))
        reader, writer = await asyncio.open_unix_connection(path)
        return FrameClient(reader, writer)

    server.open_proxy_frame = open_proxy_frame
