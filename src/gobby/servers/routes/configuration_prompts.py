"""Prompt configuration routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from gobby.prompts.models import parse_frontmatter
from gobby.servers.routes.configuration_context import ConfigurationRouteContext
from gobby.servers.routes.configuration_models import SavePromptOverrideRequest

logger = logging.getLogger(__name__)


def _normalize_variable_spec(spec: Any) -> dict[str, Any]:
    """Return the API shape for prompt variable metadata."""
    if isinstance(spec, dict):
        return {
            "type": spec.get("type", "str"),
            "required": spec.get("required", False),
            "default": spec.get("default"),
        }
    return {"type": "str", "default": spec}


def register_prompt_routes(router: APIRouter, context: ConfigurationRouteContext) -> None:
    """Register prompt listing, detail, override, and revert routes."""

    @router.get("/prompts")
    async def list_prompts() -> JSONResponse:
        """List all prompts with category, source tier, override status."""
        try:
            manager = context.get_prompt_manager()
            records = manager.list_prompts(
                project_id=context.server.services.project_id,
                enabled=True,
                limit=500,
            )

            seen: dict[str, Any] = {}
            override_names: set[str] = set()

            for record in records:
                if record.scope in ("global", "project"):
                    override_names.add(record.name)
                if record.name not in seen:
                    seen[record.name] = record

            prompts = []
            for name, record in sorted(seen.items()):
                category = name.split("/")[0] if "/" in name else "general"
                has_override = name in override_names
                source = "overridden" if has_override else "bundled"

                prompts.append(
                    {
                        "path": name,
                        "description": record.description,
                        "category": category,
                        "source": source,
                        "has_override": has_override,
                    }
                )

            categories: dict[str, int] = {}
            for prompt in prompts:
                category = str(prompt["category"])
                categories[category] = categories.get(category, 0) + 1

            return JSONResponse(
                content={
                    "prompts": prompts,
                    "categories": categories,
                    "total": len(prompts),
                }
            )
        except Exception as e:
            logger.error("Failed to list prompts: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.get("/prompts/{path:path}")
    async def get_prompt_detail(path: str) -> JSONResponse:
        """Get prompt content and frontmatter."""
        try:
            manager = context.get_prompt_manager()

            record = manager.get_by_name(path, project_id=context.server.services.project_id)
            if not record:
                raise HTTPException(status_code=404, detail=f"Prompt '{path}' not found")

            has_override = record.scope in ("global", "project")

            bundled_content = None
            if has_override:
                bundled = manager.get_bundled(path)
                if bundled:
                    bundled_content = bundled.content

            source = "overridden" if has_override else "bundled"

            return JSONResponse(
                content={
                    "path": path,
                    "description": record.description,
                    "content": record.content,
                    "source": source,
                    "has_override": has_override,
                    "bundled_content": bundled_content,
                    "variables": {
                        name: _normalize_variable_spec(spec)
                        for name, spec in (record.variables or {}).items()
                    },
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get prompt: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.put("/prompts/{path:path}")
    async def save_prompt_override(
        path: str,
        request: SavePromptOverrideRequest,
    ) -> JSONResponse:
        """Create/update a prompt override (scope='global') in the database."""
        try:
            manager = context.get_prompt_manager()

            frontmatter, body = parse_frontmatter(request.content)
            description = frontmatter.get("description", "")
            version = str(frontmatter.get("version", "1.0"))
            variables = frontmatter.get("variables")

            existing_override = manager.get_override(path)

            if existing_override:
                manager.update_prompt(
                    prompt_id=existing_override.id,
                    description=description,
                    content=body.strip() if body.strip() else request.content,
                    version=version,
                    variables=variables,
                )
            else:
                manager.create_prompt(
                    name=path,
                    description=description,
                    content=body.strip() if body.strip() else request.content,
                    version=version,
                    variables=variables,
                    scope="global",
                )

            loader = context.get_prompt_loader()
            loader.clear_cache()

            return JSONResponse(content={"ok": True})
        except Exception as e:
            logger.error("Failed to save prompt override: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.delete("/prompts/{path:path}")
    async def delete_prompt_override(path: str) -> JSONResponse:
        """Remove override (revert to bundled) by deleting the global record."""
        try:
            manager = context.get_prompt_manager()

            if not manager.delete_override(path):
                raise HTTPException(status_code=404, detail=f"No override for '{path}'")

            loader = context.get_prompt_loader()
            loader.clear_cache()

            return JSONResponse(content={"ok": True})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to delete prompt override: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e
