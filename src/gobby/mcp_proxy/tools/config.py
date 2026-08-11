"""MCP tools for the universal configuration service."""

from __future__ import annotations

import logging
from collections.abc import Callable

from gobby.config.values import ConfigValuesError, ConfigValuesService
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.config_repository import MAX_CONFIG_REVISION

logger = logging.getLogger(__name__)


def _validate_revision(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_CONFIG_REVISION:
        raise ConfigValuesError(
            "validation_error",
            f"Configuration revision must be an integer from 0 to {MAX_CONFIG_REVISION}",
            ("expected_revision",),
        )
    return value


def create_config_registry(
    service_getter: Callable[[], ConfigValuesService],
) -> InternalToolRegistry:
    """Create the public configuration MCP registry."""
    registry = InternalToolRegistry(
        name="gobby-config",
        description="Public daemon configuration schema, values, and revisioned patching",
    )

    @registry.tool(
        name="get_config_schema",
        description="Get the public daemon configuration schema.",
    )
    async def get_config_schema() -> dict[str, object]:
        return await service_getter().schema()

    @registry.tool(
        name="get_config_values",
        description="Get desired and active public daemon configuration values.",
    )
    async def get_config_values() -> dict[str, object]:
        return await service_getter().values()

    async def patch_config_values(
        expected_revision: object,
        values: dict[str, object] | None = None,
        unset: list[str] | None = None,
    ) -> dict[str, object]:
        try:
            return await service_getter().patch(
                expected_revision=_validate_revision(expected_revision),
                values=values or {},
                unset=unset or (),
            )
        except ConfigValuesError as exc:
            return exc.public_body()
        except Exception:
            logger.exception("Configuration persistence outcome is indeterminate")
            return {
                "error": {
                    "code": "persistence_indeterminate",
                    "message": "Configuration persistence outcome is indeterminate",
                    "path": [],
                    "retryable": False,
                }
            }

    registry.register(
        name="patch_config_values",
        description="Patch public daemon configuration values at an expected revision.",
        input_schema={
            "type": "object",
            "properties": {
                "expected_revision": {
                    "anyOf": [
                        {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": MAX_CONFIG_REVISION,
                        }
                    ]
                },
                "values": {"type": "object", "default": {}},
                "unset": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
            },
            "required": ["expected_revision"],
            "additionalProperties": False,
        },
        func=patch_config_values,
        brief="Patch public daemon configuration values. Requires: expected_revision",
    )

    return registry


__all__ = ["create_config_registry"]
