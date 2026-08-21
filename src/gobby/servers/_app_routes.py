"""HTTP route registration for the daemon FastAPI app."""

from typing import TYPE_CHECKING

from fastapi import FastAPI

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


def register_routes(app: FastAPI, server: "HTTPServer") -> None:
    """
    Register HTTP routes using extracted router modules.

    Args:
        app: FastAPI application instance
        server: HTTPServer instance
    """
    from gobby.servers.routes import (
        create_admin_router,
        create_agent_spawn_router,
        create_agents_router,
        create_attention_router,
        create_build_router,
        create_chat_attachments_router,
        create_chat_router,
        create_code_index_router,
        create_communications_router,
        create_configuration_router,
        create_cron_router,
        create_embeddings_router,
        create_files_router,
        create_github_triage_router,
        create_health_router,
        create_hooks_router,
        create_hub_files_proxy_router,
        create_llm_router,
        create_mcp_router,
        create_memory_dream_router,
        create_memory_router,
        create_metrics_router,
        create_observations_router,
        create_pipeline_definitions_router,
        create_pipelines_router,
        create_profiles_router,
        create_projects_router,
        create_providers_router,
        create_rules_router,
        create_runtime_config_router,
        create_runtime_handshake_router,
        create_sessions_router,
        create_skills_router,
        create_source_control_router,
        create_stages_router,
        create_tasks_router,
        create_traces_router,
        create_variable_definitions_router,
        create_voice_router,
        create_webhooks_router,
        create_wiki_code_router,
        create_wiki_router,
    )
    from gobby.servers.routes.auth import create_auth_router

    app.include_router(create_auth_router(server))
    app.include_router(create_health_router(server))
    app.include_router(create_admin_router(server))
    app.include_router(create_agent_spawn_router(server))
    app.include_router(create_agents_router(server))
    app.include_router(create_attention_router(server))
    app.include_router(create_build_router(server))
    app.include_router(create_chat_attachments_router(server))
    app.include_router(create_chat_router(server))
    app.include_router(create_sessions_router(server))
    app.include_router(create_memory_router(server))
    app.include_router(create_memory_dream_router(server))
    app.include_router(create_tasks_router(server))
    app.include_router(create_stages_router(server))
    app.include_router(create_code_index_router(server))
    app.include_router(create_cron_router(server))
    app.include_router(create_mcp_router())
    app.include_router(create_hooks_router(server))
    app.include_router(create_webhooks_router())
    # Mount definition CRUD before GET /api/pipelines/{execution_id}.
    app.include_router(create_pipeline_definitions_router(server))
    app.include_router(create_pipelines_router(server))
    app.include_router(create_files_router(server))
    app.include_router(create_hub_files_proxy_router())
    app.include_router(create_github_triage_router(server))
    app.include_router(create_projects_router(server))
    app.include_router(create_profiles_router(server))
    app.include_router(create_providers_router(server))
    app.include_router(create_skills_router(server))
    app.include_router(create_llm_router(server))
    app.include_router(create_embeddings_router(server))
    app.include_router(create_voice_router(server))
    app.include_router(create_configuration_router(server))
    app.include_router(create_runtime_handshake_router(server))
    app.include_router(create_runtime_config_router(server))
    app.include_router(create_variable_definitions_router(server))
    app.include_router(create_rules_router(server))
    app.include_router(create_source_control_router(server))
    app.include_router(create_traces_router(server))
    app.include_router(create_metrics_router(server))
    app.include_router(create_observations_router(server))
    app.include_router(create_wiki_code_router(server))
    app.include_router(create_wiki_router(server))

    app.include_router(create_communications_router(server))
