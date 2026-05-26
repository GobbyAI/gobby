"""HTTP, WebSocket, and broadcast setup for GobbyRunner."""

from __future__ import annotations

import logging
import weakref
from typing import TYPE_CHECKING

from gobby.app_context import ServiceContainer, set_app_context
from gobby.servers.http import HTTPServer
from gobby.servers.provider_models import ProviderModelCatalog
from gobby.servers.websocket.chat.runtime_manager import WebChatRuntimeManager
from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry
from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.server import WebSocketServer

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger(__name__)


def init_servers(runner: GobbyRunner) -> None:
    """Initialize HTTP server, WebSocket server, and broadcasting."""
    web_chat_session_registry = WebChatSessionRegistry()
    runner.wake_dispatcher.set_web_chat_session_registry(web_chat_session_registry)
    http_server_ref: weakref.ReferenceType[HTTPServer] | None = None

    def tool_proxy_getter() -> object | None:
        http_server = http_server_ref() if http_server_ref is not None else None
        return http_server.tool_proxy if http_server is not None else None

    services = ServiceContainer(
        config=runner.config,
        database=runner.database,
        db_executor=runner.db_executor,
        session_manager=runner.session_manager,
        task_manager=runner.task_manager,
        span_storage=runner.span_storage,
        task_sync_manager=runner.task_sync_manager,
        memory_sync_manager=runner.memory_sync_manager,
        memory_manager=runner.memory_manager,
        llm_service=runner.llm_service,
        vector_store=runner.vector_store,
        mcp_manager=runner.mcp_proxy,
        mcp_db_manager=runner.mcp_db_manager,
        metrics_manager=runner.metrics_manager,
        agent_runner=runner.agent_runner,
        message_processor=runner.message_processor,
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
        communications_manager=runner.communications_manager,
        code_indexer=runner.code_indexer,
        cron_storage=runner.cron_storage,
        cron_scheduler=runner.cron_scheduler,
        skill_manager=runner.skill_manager,
        hub_manager=runner.hub_manager,
        config_store=runner.config_store,
        provider_model_catalog=ProviderModelCatalog(runner.config),
        web_chat_runtime_manager=None,
        web_chat_session_registry=web_chat_session_registry,
        prompt_manager=runner.prompt_manager,
        dev_mode=runner._dev_mode,
        tool_proxy_getter=tool_proxy_getter,
    )

    set_app_context(services)
    if runner.cron_scheduler and getattr(runner.cron_scheduler, "executor", None):
        runner.cron_scheduler.executor.services = services

    if runner.communications_manager:
        from gobby.communications.reactions import ReactionHandler

        runner.communications_manager.reaction_handler = ReactionHandler(
            runner.communications_manager.store, services
        )

    codex_client = None
    from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter

    if CodexAdapter.is_codex_available():
        from gobby.adapters.codex_impl.client import CodexAppServerClient

        codex_client = CodexAppServerClient()
        logger.info("Codex app-server client created (will start in HTTP lifespan)")

    gemini_default_model: str | None = None
    gemini_config = getattr(runner.config.llm_providers, "gemini", None)
    if gemini_config is not None:
        gemini_default_model = gemini_config.default_model
        if not gemini_default_model:
            models = gemini_config.get_models_list()
            gemini_default_model = models[0] if models else None

    services.web_chat_runtime_manager = WebChatRuntimeManager(
        codex_client=codex_client,
        gemini_default_model=gemini_default_model,
        daemon_config=runner.config,
    )

    runner.http_server = HTTPServer(
        services=services,
        port=runner.config.daemon_port,
        test_mode=runner.config.test_mode,
        codex_client=codex_client,
    )
    http_server_ref = weakref.ref(runner.http_server)
    runner.http_server.set_runner_getter(weakref.ref(runner))

    runner.http_server.message_processor = runner.message_processor

    if runner.pipeline_executor is not None:
        runner.pipeline_executor.tool_proxy_getter = tool_proxy_getter

    runner.websocket_server = None
    if runner.config.websocket and getattr(runner.config.websocket, "enabled", True):
        websocket_config = WebSocketConfig(
            host=runner.config.bind_host,
            port=runner.config.websocket.port,
            ping_interval=runner.config.websocket.ping_interval,
            ping_timeout=runner.config.websocket.ping_timeout,
        )
        runner.websocket_server = WebSocketServer(
            config=websocket_config,
            mcp_manager=runner.mcp_proxy,
            session_manager=runner.session_manager,
            db_executor=runner.db_executor,
            daemon_config=runner.config,
            internal_manager=runner.http_server._internal_manager,
            web_chat_session_registry=web_chat_session_registry,
        )
        runner.websocket_server.web_chat_runtime_manager = services.web_chat_runtime_manager
        runner.http_server.websocket_server = runner.websocket_server
        runner.http_server.services.websocket_server = runner.websocket_server
        runner.http_server.broadcaster.websocket_server = runner.websocket_server

        if runner.message_processor:
            runner.message_processor.websocket_server = runner.websocket_server
            runner.message_processor.session_manager = runner.session_manager

        from gobby.runner_broadcasting import (
            setup_agent_event_broadcasting,
            setup_communications_event_broadcasting,
            setup_cron_event_broadcasting,
            setup_pipeline_event_broadcasting,
        )

        setup_agent_event_broadcasting(runner.websocket_server)

        if runner.pipeline_executor:
            setup_pipeline_event_broadcasting(runner.websocket_server, runner.pipeline_executor)

        if runner.cron_scheduler:
            setup_cron_event_broadcasting(runner.websocket_server, runner.cron_scheduler)

        if runner.communications_manager:
            setup_communications_event_broadcasting(
                runner.websocket_server,
                runner.communications_manager,
            )
