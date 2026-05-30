"""
Configuration routes for Gobby HTTP server.

Provides endpoints for:
- Structured config form (schema + values)
- Secrets management (encrypted API keys)
- Prompt template management (view/override/revert)
- Raw YAML editing
- Export/import configuration bundles
"""

from typing import TYPE_CHECKING

from fastapi import APIRouter

from gobby.servers.routes import configuration_validation_detection as validation_detection_routes
from gobby.servers.routes.configuration_context import ConfigurationRouteContext
from gobby.servers.routes.configuration_import_export import register_import_export_routes
from gobby.servers.routes.configuration_models import (
    ImportConfigRequest,
    SaveApprovalRulesRequest,
    SaveConfigRequest,
    SavePromptOverrideRequest,
    SaveSecretRequest,
    SaveTemplateRequest,
    SaveUISettingsRequest,
)
from gobby.servers.routes.configuration_prompts import register_prompt_routes
from gobby.servers.routes.configuration_secrets import register_secret_routes
from gobby.servers.routes.configuration_templates import register_template_routes
from gobby.servers.routes.configuration_tool_approvals import register_tool_approval_routes
from gobby.servers.routes.configuration_ui_settings import register_ui_setting_routes
from gobby.servers.routes.configuration_values import register_value_routes

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

__all__ = [
    "ImportConfigRequest",
    "SaveApprovalRulesRequest",
    "SaveConfigRequest",
    "SavePromptOverrideRequest",
    "SaveSecretRequest",
    "SaveTemplateRequest",
    "SaveUISettingsRequest",
    "create_configuration_router",
]


def create_configuration_router(server: "HTTPServer") -> APIRouter:
    """Create the configuration API router."""
    router = APIRouter(prefix="/api/config", tags=["configuration"])
    context = ConfigurationRouteContext(server)

    validation_detection_routes.register_validation_detection_routes(router, context)
    register_value_routes(router, context)
    register_template_routes(router, context)
    register_secret_routes(router, context)
    register_prompt_routes(router, context)
    register_import_export_routes(router, context)
    register_ui_setting_routes(router, context)
    register_tool_approval_routes(router, context)

    return router
