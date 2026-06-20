from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from gobby.config.embedding_keys import is_removed_embedding_config_store_key
from gobby.config.wiki_migration import migrate_legacy_wiki_roots

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig

logger = logging.getLogger(__name__)

# Pattern for environment variable substitution:
# ${VAR} - simple substitution
# ${VAR:-default} - with default value if VAR is unset or empty
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# Pattern for secret references (secrets-store-only, no env fallback):
# $secret:NAME - resolved from encrypted secrets store
SECRET_REF_PATTERN = re.compile(r"\$secret:([A-Za-z_][A-Za-z0-9_]*)")

# Keys renamed/removed from DaemonConfig that may still exist in DB config_store
_LEGACY_KEYS_TO_DROP = frozenset(
    {"_meta", "review", "task_description", "title_synthesis", "rules", "ui_settings"}
)
_REMOVED_CONFIG_STORE_KEYS = frozenset({"databases.falkordb.requirepass"})
_REMOVED_CONFIG_STORE_PREFIXES = ("local.",)
_UI_MODE_CONFIG_KEY = "ui.mode"
_CODE_INDEX_SYMBOL_SUMMARY_KEY_MIGRATIONS = {
    "code_index.summary_enabled": "code_index.symbol_summary.enabled",
    "code_index.summary_batch_size": "code_index.symbol_summary.batch_size",
    "code_index.summary_profile": "code_index.symbol_summary.profile",
    "code_index.summary_candidates": "code_index.symbol_summary.candidates",
    "code_index.summary_max_concurrency": "code_index.symbol_summary.max_concurrency",
}

# Mapping from old logging.* field names to new telemetry.* field names
_LOGGING_TO_TELEMETRY_FIELDS: dict[str, str] = {
    "level": "log_level",
    "format": "log_format",
    "client": "log_file",
    "client_error": "log_file_error",
    "hook_manager": "log_file_hook_manager",
    "mcp_server": "log_file_mcp_server",
    "mcp_client": "log_file_mcp_client",
    # These kept the same name
    "max_size_mb": "max_size_mb",
    "backup_count": "backup_count",
}

_BOOTSTRAP_BACKEND_KEYS = ("hub_backend", "database_url", "postgres_install_mode")


def expand_env_vars(
    content: str,
    secret_resolver: Callable[[str], str | None] | None = None,
) -> str:
    """
    Expand variable references in configuration content.

    Supports three syntaxes:
    - $secret:NAME - resolved exclusively from encrypted secrets store
    - ${VAR} - secrets store first (if resolver provided), then env var
    - ${VAR:-default} - same as above, with fallback default

    Args:
        content: Configuration file content as string
        secret_resolver: Optional callable that takes a variable name and returns
            the decrypted secret value, or None if not found.

    Returns:
        Content with variables expanded
    """

    # Pass 1: Resolve $secret:NAME references (secrets-store-only)
    if secret_resolver is not None:

        def replace_secret(match: re.Match[str]) -> str:
            name = match.group(1)
            try:
                value = secret_resolver(name)
                if value is not None:
                    return value
            except Exception as e:
                logger.debug(f"Secret resolver failed for '$secret:{name}': {e}")
            logger.warning(
                f"Unresolved secret '$secret:{name}' in config - not found in secrets store"
            )
            return match.group(0)

        content = SECRET_REF_PATTERN.sub(replace_secret, content)

    # Pass 2: Resolve ${VAR} references (secrets first, then env vars)
    def replace_env(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default_value = match.group(2)  # None if no default specified

        # 1. Try secret store first
        if secret_resolver is not None:
            try:
                secret_value = secret_resolver(var_name)
                if secret_value is not None and secret_value != "":
                    return secret_value
            except Exception as e:
                logger.debug(f"Secret resolver failed for '{var_name}': {e}")

        # 2. Try environment variable
        env_value = os.environ.get(var_name)
        if env_value is not None and env_value != "":
            return env_value

        # 3. Use default if provided
        if default_value is not None:
            return default_value

        # 4. Unresolved: warn and leave unchanged
        logger.warning(
            f"Unresolved variable '${{{var_name}}}' in config "
            f"- not found in secrets store or environment"
        )
        return match.group(0)

    return ENV_VAR_PATTERN.sub(replace_env, content)


def _ensure_config_mapping(data: Any, config_path: Path) -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(
            "Config file must contain a mapping/object at the top level, "
            f"got {type(data).__name__}: {config_path}"
        )
    return data


def load_yaml(
    config_file: str,
    secret_resolver: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """
    Load YAML or JSON configuration file.

    Args:
        config_file: Path to YAML or JSON configuration file
        secret_resolver: Optional callable for resolving secrets (checked before env vars)

    Returns:
        Dictionary with parsed YAML/JSON content

    Raises:
        ValueError: If YAML/JSON is invalid or file format is wrong
    """
    config_path = Path(config_file).expanduser()

    if not config_path.exists():
        return {}

    # Validate file extension matches format
    file_ext = config_path.suffix.lower()
    if file_ext not in [".yaml", ".yml", ".json"]:
        raise ValueError(
            f"Config file must have .yaml, .yml, or .json extension, got: {file_ext}\n"
            f"File: {config_path}"
        )

    try:
        with open(config_path) as f:
            content = f.read()

        # Expand variables (secrets first if resolver provided, then env vars)
        content = expand_env_vars(content, secret_resolver=secret_resolver)

        # Handle JSON files
        if file_ext == ".json":
            data = json.loads(content) if content.strip() else {}
            return _ensure_config_mapping(data, config_path)

        # Handle YAML files
        data = yaml.safe_load(content)
        return _ensure_config_mapping(data, config_path)

    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in config file: {e}") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in config file: {e}") from e


def apply_cli_overrides(
    config_dict: dict[str, Any],
    cli_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Apply CLI argument overrides to config dictionary.

    Args:
        config_dict: Configuration dictionary
        cli_overrides: Dictionary of CLI overrides

    Returns:
        Configuration dictionary with CLI overrides applied
    """
    if cli_overrides is None:
        return config_dict

    # Apply overrides at top level
    for key, value in cli_overrides.items():
        if "." in key:
            # Handle nested keys like "logging.level"
            parts = key.split(".")
            current = config_dict
            for index, part in enumerate(parts[:-1]):
                if part not in current:
                    current[part] = {}
                elif not isinstance(current[part], dict):
                    path = ".".join(parts[: index + 1])
                    raise ValueError(f"Cannot apply override {key!r}: {path!r} is not a mapping")
                current = current[part]
            current[parts[-1]] = value
        else:
            config_dict[key] = value

    return config_dict


def deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> None:
    """Deep-merge updates into base dict (in-place)."""
    for key, value in updates.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            deep_merge(base[key], value)
        else:
            base[key] = value


def _reject_removed_file_config_sections(file_dict: dict[str, Any], config_path: Path) -> None:
    """Reject config-file-only surfaces that must not silently round-trip."""
    if "llm_providers" in file_dict:
        raise ValueError(
            "llm_providers config has been removed. Remove the top-level llm_providers "
            f"section from {config_path} and use feature configs and provider discovery instead."
        )


def _resolve_config_values(
    d: dict[str, Any],
    secret_resolver: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Walk a config dict and resolve $secret:NAME / ${VAR} patterns in string values."""
    result: dict[str, Any] = {}
    for key, value in d.items():
        if isinstance(value, dict):
            result[key] = _resolve_config_values(value, secret_resolver)
        elif isinstance(value, list):
            result[key] = [
                expand_env_vars(item, secret_resolver=secret_resolver)
                if isinstance(item, str)
                else _resolve_config_values(item, secret_resolver)
                if isinstance(item, dict)
                else item
                for item in value
            ]
        elif isinstance(value, str):
            result[key] = expand_env_vars(value, secret_resolver=secret_resolver)
        else:
            result[key] = value
    return result


def _migrate_legacy_config(config_dict: dict[str, Any]) -> dict[str, Any]:
    """Migrate legacy config keys that were renamed or removed.

    Handles:
    - logging.* -> telemetry.* (field name remapping)
    - Removal of _meta, title_synthesis, rules, ui_settings
    - wiki.roots entries ending in .gobby/wiki -> sibling gobby-wiki
    """
    # Drop removed top-level keys
    for key in _LEGACY_KEYS_TO_DROP:
        config_dict.pop(key, None)

    # Migrate logging -> telemetry
    if "logging" in config_dict:
        old_logging = config_dict.pop("logging")
        if isinstance(old_logging, dict):
            telemetry = config_dict.setdefault("telemetry", {})
            for old_field, new_field in _LOGGING_TO_TELEMETRY_FIELDS.items():
                if old_field in old_logging and new_field not in telemetry:
                    telemetry[new_field] = old_logging[old_field]

    for key in ("gobby_tasks", "gobby-tasks"):
        gobby_tasks = config_dict.get(key)
        if isinstance(gobby_tasks, dict):
            gobby_tasks.pop("enrichment", None)

    migrate_legacy_wiki_roots(config_dict)

    return config_dict


def _drop_legacy_embedding_config_store_keys(
    flat_config: dict[str, Any], config_store: Any | None
) -> dict[str, Any]:
    """Ignore stale embedding config_store keys after the hard namespace cut."""
    legacy_keys = sorted(key for key in flat_config if is_removed_embedding_config_store_key(key))
    if not legacy_keys:
        return flat_config

    logger.warning(
        "Ignoring stale embedding config_store keys after hard namespace cut: %s",
        ", ".join(legacy_keys),
    )
    migrated = dict(flat_config)
    for key in legacy_keys:
        migrated.pop(key, None)
        delete = getattr(config_store, "delete", None)
        if callable(delete):
            try:
                delete(key)
            except Exception as exc:
                logger.debug("Failed to delete stale embedding config key %s: %s", key, exc)
    return migrated


def _drop_removed_config_store_keys(
    flat_config: dict[str, Any], config_store: Any | None
) -> dict[str, Any]:
    removed_keys = sorted(
        key
        for key in flat_config
        if key in _REMOVED_CONFIG_STORE_KEYS
        or key == "local"
        or key.startswith(_REMOVED_CONFIG_STORE_PREFIXES)
    )
    if not removed_keys:
        return flat_config

    logger.warning("Ignoring removed config_store keys: %s", ", ".join(removed_keys))
    migrated = dict(flat_config)
    for key in removed_keys:
        migrated.pop(key, None)
        delete = getattr(config_store, "delete", None)
        if callable(delete):
            try:
                delete(key)
            except Exception as exc:
                logger.debug("Failed to delete removed config key %s: %s", key, exc)
    return migrated


def _migrate_code_index_symbol_summary_config_store_keys(
    flat_config: dict[str, Any], config_store: Any | None
) -> dict[str, Any]:
    """Move persisted code-index summary rows into the nested feature config."""
    old_keys = sorted(
        key for key in flat_config if key in _CODE_INDEX_SYMBOL_SUMMARY_KEY_MIGRATIONS
    )
    if not old_keys:
        return flat_config

    migrated = dict(flat_config)
    keys_to_delete: list[str] = []
    set_value = getattr(config_store, "set", None)
    for old_key in old_keys:
        new_key = _CODE_INDEX_SYMBOL_SUMMARY_KEY_MIGRATIONS[old_key]
        value = migrated[old_key]
        if new_key not in migrated:
            if callable(set_value):
                try:
                    set_value(new_key, value, source="migration")
                except Exception as exc:
                    logger.debug(
                        "Failed to persist migrated config key %s: %s",
                        new_key,
                        exc,
                    )
                    continue
            migrated[new_key] = value
        migrated.pop(old_key, None)
        keys_to_delete.append(old_key)

    if not keys_to_delete:
        return migrated

    delete = getattr(config_store, "delete", None)
    if callable(delete):
        for key in keys_to_delete:
            try:
                delete(key)
            except Exception as exc:
                logger.debug("Failed to delete migrated config key %s: %s", key, exc)

    logger.info(
        "Migrated code-index symbol summary config keys: %s",
        ", ".join(keys_to_delete),
    )
    return migrated


def _migrate_default_ui_mode_config_store_row(
    flat_config: dict[str, Any], config_store: Any | None
) -> dict[str, Any]:
    """Upgrade defaults-seeded ui.mode rows from production to auto."""
    if flat_config.get(_UI_MODE_CONFIG_KEY) != "production":
        return flat_config

    db = getattr(config_store, "db", None)
    execute = getattr(db, "execute", None)
    if not callable(execute):
        return flat_config

    try:
        cursor = execute(
            """UPDATE config_store
               SET value = %s, updated_at = %s
               WHERE key = %s AND source = %s AND value = %s""",
            (
                '"auto"',
                datetime.now(UTC).isoformat(),
                _UI_MODE_CONFIG_KEY,
                "defaults",
                '"production"',
            ),
        )
    except Exception as exc:
        logger.debug("Failed to migrate defaults-seeded ui.mode config row: %s", exc)
        return flat_config

    if not getattr(cursor, "rowcount", 0):
        return flat_config

    migrated = dict(flat_config)
    migrated[_UI_MODE_CONFIG_KEY] = "auto"
    logger.info("Migrated defaults-seeded ui.mode config row from production to auto")
    return migrated


def _restore_bootstrap_backend_selection(config_dict: dict[str, Any], bootstrap: Any) -> None:
    for key in _BOOTSTRAP_BACKEND_KEYS:
        config_dict[key] = getattr(bootstrap, key)


def export_config_to_yaml(config: DaemonConfig, config_file: str | None = None) -> None:
    """
    Export configuration to YAML file (for backup/migration).

    This is NOT used at runtime - runtime config comes from DB + Pydantic defaults.
    Use this for export/import workflows or one-time migration snapshots.

    Args:
        config: DaemonConfig instance to export
        config_file: Path to YAML export file (default: ~/.gobby/config.yaml)

    Raises:
        OSError: If file operations fail
        RuntimeError: If called with production path during tests (GOBBY_TEST_PROTECT=1)
    """
    if config_file is None:
        config_file = "~/.gobby/config.yaml"

    config_path = Path(config_file).expanduser()

    # Block writes to production config during tests
    if os.environ.get("GOBBY_TEST_PROTECT") == "1":
        real_gobby_home = Path("~/.gobby").expanduser().resolve()
        try:
            if config_path.resolve().is_relative_to(real_gobby_home):
                raise RuntimeError(
                    f"export_config_to_yaml() would write to production path "
                    f"{config_path} during tests."
                )
        except (ValueError, OSError):
            pass

    # Ensure directory exists
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Convert config to dict, excluding None values to keep file clean
    # mode="json" ensures Path objects are converted to strings for YAML serialization
    config_dict = config.model_dump(mode="json", exclude_none=True, by_alias=True)

    # Write with owner-only permissions before any data is emitted.
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            fd = -1
            yaml.safe_dump(config_dict, f, default_flow_style=False, sort_keys=False)
    finally:
        if fd != -1:
            os.close(fd)
