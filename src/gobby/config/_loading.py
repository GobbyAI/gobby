from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from gobby.config.voice_secrets import mask_voice_audio_api_keys
from gobby.storage.secret_names import SECRET_REF_PATTERN
from gobby.utils.env import is_test_protect_enabled

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.config.bootstrap import BootstrapConfig

logger = logging.getLogger(__name__)

# Pattern for environment variable substitution:
# ${VAR} - simple substitution
# ${VAR:-default} - with default value if VAR is unset or empty
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_BOOTSTRAP_PRE_DATABASE_KEYS = (
    "datastore_mode",
    "database_url",
    "postgres_pool",
)
_MASKED_REFERENCE = "********"


def _mask_reference_values(
    value: object,
    canonical_prefix: tuple[str, ...] = (),
) -> object:
    """Mask every scalar reference-secrecy value in a dumped config tree."""
    # app imports this loader, while registry derives its schema from app.
    # Resolve the registry only when the export-only path actually runs.
    from gobby.config.registry import (
        CONFIG_REGISTRY,
        ConfigSecrecy,
        UnknownConfigKeyError,
        config_key_secrecy,
        encode_dynamic_segment,
    )

    if isinstance(value, list):
        return [_mask_reference_values(item, canonical_prefix) for item in value]
    if not isinstance(value, dict):
        return value

    masked: dict[object, object] = {}
    for key, child in value.items():
        if not isinstance(key, str):
            masked[key] = child
            continue
        segment = (
            encode_dynamic_segment(key)
            if CONFIG_REGISTRY.dynamic_segment_follows(canonical_prefix)
            else key
        )
        child_prefix = (*canonical_prefix, segment)
        try:
            spec = CONFIG_REGISTRY.resolve(".".join(child_prefix))
        except UnknownConfigKeyError:
            spec = None
        if (
            spec is not None
            and config_key_secrecy(spec, ".".join(child_prefix)) is ConfigSecrecy.REFERENCE
        ):
            masked[key] = _MASKED_REFERENCE
        else:
            masked[key] = _mask_reference_values(child, child_prefix)
    return masked


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

    protected_values: list[str] = []

    def protect_value(value: str) -> str:
        placeholder = f"\0GOBBY_CONFIG_VALUE_{len(protected_values)}\0"
        protected_values.append(value)
        return placeholder

    # Pass 1: Resolve ${VAR} references. Secret-derived values are protected so
    # later passes never interpret their contents as config interpolation syntax.
    def replace_env(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default_value = match.group(2)  # None if no default specified

        # 1. Try secret store first
        if secret_resolver is not None:
            try:
                secret_value = secret_resolver(var_name)
                if secret_value is not None and secret_value != "":  # nosec B105
                    return protect_value(secret_value)
            except Exception as e:
                logger.debug("Secret resolver failed for '%s': %s", var_name, e)

        # 2. Try environment variable
        env_value = os.environ.get(var_name)
        if env_value is not None and env_value != "":
            return env_value

        # 3. Use default if provided
        if default_value is not None:
            return default_value

        # 4. Unresolved: warn and leave unchanged
        logger.warning(
            "Unresolved variable '${%s}' in config - not found in secrets store or environment",
            var_name,
        )
        return match.group(0)

    content = ENV_VAR_PATTERN.sub(replace_env, content)

    # Pass 2: Resolve $secret:NAME references (secrets-store-only).
    if secret_resolver is not None:

        def replace_secret(match: re.Match[str]) -> str:
            name = match.group(1)
            try:
                value = secret_resolver(name)
                if value is not None:
                    return protect_value(value)
            except Exception as e:
                logger.debug("Secret resolver failed for '$secret:%s': %s", name, e)
            logger.warning(
                "Unresolved secret '$secret:%s' in config - not found in secrets store", name
            )
            return match.group(0)

        content = SECRET_REF_PATTERN.sub(replace_secret, content)

    for index, value in enumerate(protected_values):
        content = content.replace(f"\0GOBBY_CONFIG_VALUE_{index}\0", value)

    return content


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


def bootstrap_overlaid_config(
    candidate: DaemonConfig,
    bootstrap: BootstrapConfig,
) -> DaemonConfig:
    """Overlay bootstrap-owned facts onto a DB-backed configuration projection."""
    from gobby.config.app import DaemonConfig

    merged = candidate.model_dump(mode="python", by_alias=False)
    deep_merge(merged, bootstrap.to_config_dict())
    return DaemonConfig.model_validate(merged)


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


def _restore_bootstrap_pre_database_settings(config_dict: dict[str, Any], bootstrap: Any) -> None:
    for key in _BOOTSTRAP_PRE_DATABASE_KEYS:
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
    if is_test_protect_enabled():
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
    masked_config = _mask_reference_values(config_dict)
    if not isinstance(masked_config, dict):
        raise TypeError("Daemon configuration must serialize as a mapping")
    config_dict = mask_voice_audio_api_keys(masked_config)

    fd, temp_name = tempfile.mkstemp(
        dir=config_path.parent,
        prefix=f".{config_path.name}.",
        suffix=".tmp",
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            yaml.safe_dump(config_dict, f, default_flow_style=False, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, config_path)

        directory_fd = os.open(
            config_path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        if fd != -1:
            os.close(fd)
