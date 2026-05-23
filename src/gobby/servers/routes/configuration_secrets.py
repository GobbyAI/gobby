"""Secret handling helpers for configuration routes."""

from __future__ import annotations

from typing import Any

from gobby.storage.config_store import ConfigStore, config_key_to_secret_name, is_secret_key_name

MASKED_SECRET = "********"
FALKOR_REQUIREPASS_KEY = "databases.falkordb.requirepass"
FALKOR_RESTART_HINT = (
    "Run `gobby restart` for the new FalkorDB password to take effect on the running container."
)


def mask_secret_value(key: str, value: Any) -> Any:
    if is_secret_key_name(key) and value not in (None, ""):
        return MASKED_SECRET
    return value


def mask_secret_values(flat: dict[str, Any]) -> dict[str, Any]:
    return {key: mask_secret_value(key, value) for key, value in flat.items()}


def add_restart_hint(response: dict[str, Any], touched_keys: set[str]) -> None:
    if FALKOR_REQUIREPASS_KEY in touched_keys:
        response["restart_hint"] = FALKOR_RESTART_HINT


def is_secret_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("$secret:")


def mark_secret_keys(config_store: ConfigStore, keys: set[str]) -> None:
    if not keys:
        return
    placeholders = ",".join("?" for _ in keys)
    config_store.db.execute(
        f"UPDATE config_store SET is_secret = 1 WHERE key IN ({placeholders})",
        tuple(sorted(keys)),
    )


def delete_all_except(config_store: ConfigStore, preserved_keys: set[str]) -> int:
    if not preserved_keys:
        return config_store.delete_all()
    placeholders = ",".join("?" for _ in preserved_keys)
    cursor = config_store.db.execute(
        f"DELETE FROM config_store WHERE key NOT IN ({placeholders})",
        tuple(sorted(preserved_keys)),
    )
    return cursor.rowcount or 0


def partition_config_entries(
    flat_config: dict[str, Any],
    config_secret_keys: set[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    secret_references: dict[str, Any] = {}
    secret_values: dict[str, Any] = {}
    plain_values: dict[str, Any] = {}

    for key, value in flat_config.items():
        is_secret = key in config_secret_keys or is_secret_key_name(key)
        if is_secret and is_secret_reference(value):
            secret_references[key] = value
        elif is_secret:
            secret_values[key] = value
        else:
            plain_values[key] = value

    return secret_references, secret_values, plain_values


def validation_flat_for_secret_entries(
    flat_config: dict[str, Any],
    secret_value_keys: set[str],
) -> dict[str, Any]:
    validation_flat = dict(flat_config)
    for key in secret_value_keys:
        value = validation_flat.get(key)
        if value is None or value == "":
            validation_flat.pop(key, None)
        else:
            validation_flat[key] = f"$secret:{config_key_to_secret_name(key)}"
    return validation_flat
