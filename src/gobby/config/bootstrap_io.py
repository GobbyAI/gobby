"""Shared bootstrap.yaml path and atomic file helpers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from gobby.paths import get_gobby_home
from gobby.utils.durable_file import durable_replace_text, exclusive_file_lock


def bootstrap_path(gobby_home: Path | None = None) -> Path:
    return (gobby_home or get_gobby_home()) / "bootstrap.yaml"


def read_bootstrap_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return read_present_bootstrap_mapping(path)


def read_present_bootstrap_mapping(path: Path) -> dict[str, Any]:
    """Read a present bootstrap file. Fail closed on any I/O or parse error."""
    from gobby.config.bootstrap import BootstrapConfigError

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BootstrapConfigError(f"cannot read bootstrap.yaml: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BootstrapConfigError("bootstrap.yaml is not valid UTF-8") from exc
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise BootstrapConfigError("bootstrap.yaml is not valid YAML") from exc
    if not isinstance(loaded, dict):
        raise BootstrapConfigError("bootstrap.yaml must contain a YAML mapping")
    return dict(loaded)


def write_bootstrap_yaml(path: Path, data: dict[str, Any]) -> None:
    with exclusive_file_lock(path):
        publish_bootstrap_yaml_locked(path, data)


def update_bootstrap_yaml(
    path: Path,
    updater: Callable[[dict[str, Any]], None],
) -> None:
    with exclusive_file_lock(path):
        data = read_bootstrap_yaml(path)
        updater(data)
        publish_bootstrap_yaml_locked(path, data)


def inject_local_files_home(path: Path, files_home: str | Path) -> None:
    """Upgrade a present local mapping without calling load_bootstrap."""
    from gobby.config.bootstrap import validate_existing_files_home

    validated = validate_existing_files_home(files_home)
    with exclusive_file_lock(path):
        data = read_bootstrap_yaml(path)
        data["files_home"] = str(validated)
        data.setdefault("datastore_mode", "local")
        publish_bootstrap_yaml_locked(path, data)


def publish_bootstrap_yaml_locked(path: Path, data: dict[str, Any]) -> None:
    """Validate and durably replace ``path``. Caller must hold the sidecar lock."""
    from gobby.config.bootstrap import bootstrap_from_mapping

    existing = read_bootstrap_yaml(path) if path.exists() else {}
    merged = _merge_owner_fields(existing, dict(data))
    config = bootstrap_from_mapping(merged)
    if config.files_home:
        merged["files_home"] = config.files_home
    else:
        merged.pop("files_home", None)
    if config.hub_daemon_url:
        merged["hub_daemon_url"] = config.hub_daemon_url
    else:
        merged.pop("hub_daemon_url", None)
    payload = yaml.safe_dump(merged, default_flow_style=False, sort_keys=False)
    durable_replace_text(path, payload, mode=0o600)


def _merge_owner_fields(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(incoming)
    mode = merged.get("datastore_mode", existing.get("datastore_mode", "local"))
    if mode == "remote":
        if merged.get("hub_daemon_url") in (None, "") and existing.get("hub_daemon_url") not in (
            None,
            "",
        ):
            merged["hub_daemon_url"] = existing["hub_daemon_url"]
        merged.pop("files_home", None)
        return merged
    if merged.get("files_home") in (None, "") and existing.get("files_home") not in (None, ""):
        merged["files_home"] = existing["files_home"]
    merged.pop("hub_daemon_url", None)
    return merged
