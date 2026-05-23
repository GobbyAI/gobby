"""
Internal MCP tools for daemon configuration.

Exposes functionality for:
- get_config(key): Get a config value by dotted key
- get_config_section(prefix): Get an entire section as nested dict
- set_config(key, value): Set a config value by dotted key
- set_config_batch(entries): Set multiple keys atomically (validates once)
- delete_config(key): Delete a config override by dotted key
- list_config_keys(prefix?): List all config keys
- ensure_defaults(section): Populate missing keys from Pydantic defaults
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.config_store import (
    config_key_to_secret_name,
    flatten_config,
    is_secret_key_name,
    unflatten_config,
)

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.storage.config_store import ConfigStore
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

__all__ = ["create_config_registry"]

_MASKED_SECRET = "********"
_FALKOR_REQUIREPASS_KEY = "databases.falkordb.requirepass"
_FALKOR_RESTART_HINT = (
    "Run `gobby restart` for the new FalkorDB password to take effect on the running container."
)


def _mask_secret_value(key: str, value: Any) -> Any:
    if is_secret_key_name(key) and value not in (None, ""):
        return _MASKED_SECRET
    return value


def _validate_falkordb_secret(key: str, value: Any) -> None:
    if key != _FALKOR_REQUIREPASS_KEY:
        return
    from gobby.config.persistence import validate_falkordb_password

    validate_falkordb_password(str(value))


def _add_restart_metadata(result: dict[str, Any], touched_keys: Iterable[str]) -> None:
    if _FALKOR_REQUIREPASS_KEY in set(touched_keys):
        result["requires_restart"] = True
        result["restart_hint"] = _FALKOR_RESTART_HINT


def create_config_registry(
    config: DaemonConfig,
    config_store: ConfigStore,
    config_setter: Callable[[DaemonConfig], None],
    db: HubDatabase | None = None,
) -> InternalToolRegistry:
    """
    Create a config tool registry for reading/writing daemon configuration.

    Args:
        config: Current in-memory DaemonConfig
        config_store: DB-backed config key-value store
        config_setter: Callback to update in-memory config on ServiceContainer
        db: Database for SecretStore access (optional, enables is_secret support)

    Returns:
        InternalToolRegistry with config tools registered
    """
    registry = InternalToolRegistry(
        name="gobby-config",
        description=(
            "Daemon configuration - get_config, get_config_section, set_config, "
            "set_config_batch, delete_config, list_config_keys, ensure_defaults"
        ),
    )

    # Mutable reference so tools always read the latest config
    _state = {"config": config}

    def _current_config() -> DaemonConfig:
        return _state["config"]

    def _flat_config() -> dict[str, Any]:
        """Flatten current in-memory config to dotted keys."""
        return flatten_config(_current_config().model_dump(mode="json"))

    @registry.tool(
        name="get_config",
        description="Get a config value by dotted key (e.g. 'skills.hubs.clawdhub.type'). Reads from in-memory config.",
    )
    def get_config(key: str) -> dict[str, Any]:
        """Get a single config value by dotted key."""
        flat = _flat_config()
        if key in flat:
            return {"success": True, "key": key, "value": _mask_secret_value(key, flat[key])}
        return {"success": False, "error": f"Key '{key}' not found in config"}

    @registry.tool(
        name="get_config_section",
        description="Get an entire config section as nested dict (e.g. 'skills.hubs'). Filters by prefix from in-memory config.",
    )
    def get_config_section(prefix: str) -> dict[str, Any]:
        """Get a config section filtered by dotted-path prefix."""
        flat = _flat_config()
        # Filter keys matching the prefix (exact prefix + '.' boundary)
        section_prefix = prefix + "."
        filtered = {
            k[len(section_prefix) :]: _mask_secret_value(k, v)
            for k, v in flat.items()
            if k.startswith(section_prefix)
        }
        # Also include exact match
        if prefix in flat:
            return {
                "success": True,
                "prefix": prefix,
                "value": _mask_secret_value(prefix, flat[prefix]),
            }
        if not filtered:
            return {"success": False, "error": f"No keys found under prefix '{prefix}'"}
        nested = unflatten_config(filtered)
        return {"success": True, "prefix": prefix, "section": nested}

    @registry.tool(
        name="set_config",
        description="Set a config value by dotted key. Validates via DaemonConfig, persists to DB, and updates in-memory config. Pass is_secret=True to encrypt the value.",
    )
    def set_config(key: str, value: Any, is_secret: bool = False) -> dict[str, Any]:
        """Set a config value. Validates, persists to DB, updates in-memory.

        If ``is_secret`` is True, the value is encrypted via SecretStore and
        a ``$secret:`` reference is stored in config_store.
        """
        if isinstance(value, dict | list):
            return {
                "success": False,
                "error": f"Cannot set '{key}' to a {type(value).__name__}. "
                "Use dotted keys to set nested values (e.g. 'section.key').",
            }

        from gobby.config.app import DaemonConfig as DaemonConfigCls
        from gobby.config.app import deep_merge

        try:
            effective_is_secret = is_secret or is_secret_key_name(key)
            if effective_is_secret and db is None:
                return {
                    "success": False,
                    "error": f"Cannot store '{key}' as secret — database not available. "
                    "Secrets require database for encryption.",
                }
            if effective_is_secret:
                _validate_falkordb_secret(key, value)

            # For secret values, validate with the $secret: ref placeholder.
            if effective_is_secret:
                ref = f"$secret:{config_key_to_secret_name(key)}"
                validation_value = ref
            else:
                validation_value = value

            # Build a nested dict from the dotted key
            update_nested = unflatten_config({key: validation_value})

            # Deep-merge into current config dict
            current_dict = _current_config().model_dump(mode="json")
            deep_merge(current_dict, update_nested)

            # Validate by constructing a new DaemonConfig
            new_config = DaemonConfigCls(**current_dict)

            if effective_is_secret:
                actual_nested = unflatten_config({key: value})
                actual_dict = _current_config().model_dump(mode="json")
                deep_merge(actual_dict, actual_nested)
                new_config = DaemonConfigCls(**actual_dict)

            # Persist to DB
            if effective_is_secret and db is not None:
                from gobby.storage.secrets import SecretStore as SecretStoreCls

                secret_store = SecretStoreCls(db)
                config_store.set_secret(key, str(value), secret_store, source="mcp")
            else:
                config_store.set(key, value, source="mcp")

            _state["config"] = new_config
            config_setter(new_config)

            result: dict[str, Any] = {"success": True, "key": key}
            if effective_is_secret:
                result["stored_as"] = "encrypted_secret"
            else:
                result["value"] = value
            _add_restart_metadata(result, [key])
            return result
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.exception(f"Failed to set config key '{key}'")
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="set_config_batch",
        description=(
            "Set multiple config keys atomically. Validates all keys together "
            "before persisting — required when a config section has multiple "
            "required fields (e.g. 'local' needs both url and model). "
            "Pass a list of {key, value} entries."
        ),
    )
    def set_config_batch(entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Set multiple config values in one validated, atomic operation.

        Each entry is ``{"key": "dotted.key", "value": <any scalar>}``.
        All entries are merged, validated via DaemonConfig once, then
        persisted together via ``config_store.set_many()``.
        """
        if not entries:
            return {"success": False, "error": "entries list is empty"}

        from gobby.config.app import DaemonConfig as DaemonConfigCls
        from gobby.config.app import deep_merge

        try:
            # Collect and validate entry shapes
            flat_updates: dict[str, Any] = {}
            explicit_secret_keys: set[str] = set()
            for entry in entries:
                key = entry.get("key")
                value = entry.get("value")
                if not key or not isinstance(key, str):
                    return {"success": False, "error": f"Invalid entry — 'key' required: {entry}"}
                if isinstance(value, dict | list):
                    return {
                        "success": False,
                        "error": f"Cannot set '{key}' to a {type(value).__name__}. "
                        "Use dotted keys for nested values.",
                    }
                flat_updates[key] = value
                if bool(entry.get("is_secret", False)):
                    explicit_secret_keys.add(key)

            secret_keys = {
                key
                for key in flat_updates
                if key in explicit_secret_keys or is_secret_key_name(key)
            }
            plain_updates = {
                key: value for key, value in flat_updates.items() if key not in secret_keys
            }
            secret_updates = {key: flat_updates[key] for key in secret_keys}
            if secret_updates and db is None:
                return {
                    "success": False,
                    "error": "Cannot store secret config keys — database not available. "
                    "Secrets require database for encryption.",
                }
            for key, value in secret_updates.items():
                _validate_falkordb_secret(key, value)

            # Unflatten all keys → nested dict, merge into current config
            validation_updates = dict(flat_updates)
            for key in secret_keys:
                validation_updates[key] = f"$secret:{config_key_to_secret_name(key)}"
            update_nested = unflatten_config(validation_updates)
            current_dict = _current_config().model_dump(mode="json")
            deep_merge(current_dict, update_nested)

            # Validate by constructing a new DaemonConfig
            DaemonConfigCls(**current_dict)

            actual_dict = _current_config().model_dump(mode="json")
            deep_merge(actual_dict, unflatten_config(flat_updates))
            new_config = DaemonConfigCls(**actual_dict)

            # Persist all keys atomically
            if secret_updates and db is not None:
                from gobby.storage.secrets import SecretStore as SecretStoreCls

                secret_store = SecretStoreCls(db)
                with db.transaction():
                    for key, value in secret_updates.items():
                        config_store.set_secret(key, str(value), secret_store, source="mcp")
                    if plain_updates:
                        config_store.set_many(plain_updates, source="mcp")
            else:
                config_store.set_many(plain_updates, source="mcp")

            # Update in-memory config
            _state["config"] = new_config
            config_setter(new_config)

            result: dict[str, Any] = {
                "success": True,
                "keys_set": sorted(flat_updates.keys()),
                "count": len(flat_updates),
            }
            _add_restart_metadata(result, secret_keys)
            return result
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.exception("Failed to set config batch")
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="delete_config",
        description=(
            "Delete a config override by dotted key. Removes the row from "
            "config_store and, if the key was stored as a secret, also clears "
            "the encrypted blob from the secrets table (atomic). Validates "
            "that the resulting DaemonConfig is still valid (Pydantic defaults "
            "fill in for the removed key)."
        ),
    )
    def delete_config(key: str) -> dict[str, Any]:
        """Delete a config override, clearing secret storage when needed."""
        from gobby.config.app import DaemonConfig as DaemonConfigCls

        try:
            override_keys = set(config_store.list_keys())
            if key not in override_keys:
                return {
                    "success": False,
                    "error": f"Key '{key}' not found in config_store (no override to delete)",
                }

            secret_keys = set(config_store.get_secret_keys())
            had_secret = key in secret_keys

            # Validate the post-delete state before mutating DB or memory.
            flat = _flat_config()
            flat.pop(key, None)
            remaining_override_keys = override_keys - {key}
            defaults_flat = flatten_config(DaemonConfigCls().model_dump(mode="json"))
            parent_prefix = key.rsplit(".", 1)[0] + "." if "." in key else ""
            if (
                parent_prefix
                and not any(k.startswith(parent_prefix) for k in remaining_override_keys)
                and not any(k.startswith(parent_prefix) for k in defaults_flat)
            ):
                flat = {k: v for k, v in flat.items() if not k.startswith(parent_prefix)}
            new_config = DaemonConfigCls(**unflatten_config(flat))

            if had_secret:
                if db is None:
                    return {
                        "success": False,
                        "error": (
                            f"Cannot clear secret for '{key}' — database not "
                            "available. Secret deletion requires database access."
                        ),
                    }
                from gobby.storage.secrets import SecretStore as SecretStoreCls

                secret_store = SecretStoreCls(db)
                config_store.clear_secret(key, secret_store)
            else:
                config_store.delete(key)

            _state["config"] = new_config
            config_setter(new_config)

            return {
                "success": True,
                "key": key,
                "deleted": True,
                "had_secret": had_secret,
            }
        except Exception as e:
            logger.exception(f"Failed to delete config key '{key}'")
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="list_config_keys",
        description="List all config keys stored in the database, optionally filtered by prefix.",
    )
    def list_config_keys(prefix: str | None = None) -> dict[str, Any]:
        """List config keys from the DB, optionally filtered by prefix."""
        keys = config_store.list_keys(prefix=prefix)
        return {"success": True, "count": len(keys), "keys": keys}

    @registry.tool(
        name="ensure_defaults",
        description="Populate missing config keys from Pydantic defaults for a given section prefix. Useful for bootstrapping config on existing installs.",
    )
    def ensure_defaults(section: str) -> dict[str, Any]:
        """For a section prefix, insert Pydantic default values for any keys not already in DB."""
        from gobby.config.app import DaemonConfig as DaemonConfigCls

        try:
            # Get all defaults from a fresh DaemonConfig
            defaults_flat = flatten_config(DaemonConfigCls().model_dump(mode="json"))

            # Filter to the requested section
            section_prefix = section + "."
            section_defaults = {
                k: v
                for k, v in defaults_flat.items()
                if k.startswith(section_prefix) or k == section
            }

            if not section_defaults:
                return {
                    "success": False,
                    "error": f"No default keys found for section '{section}'",
                }

            # Find which keys are already in DB
            existing_keys = set(config_store.list_keys(prefix=section))

            # Only insert missing ones
            missing = {k: v for k, v in section_defaults.items() if k not in existing_keys}

            if not missing:
                return {
                    "success": True,
                    "message": f"All {len(section_defaults)} keys already present for '{section}'",
                    "inserted": 0,
                }

            count = config_store.set_many(missing, source="defaults")
            return {
                "success": True,
                "inserted": count,
                "total_section_keys": len(section_defaults),
                "keys_inserted": sorted(missing.keys()),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    return registry
