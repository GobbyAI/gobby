"""Shared bootstrap.yaml path and atomic file helpers."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml


def default_gobby_home() -> Path:
    return Path("~/.gobby").expanduser()


def bootstrap_path(gobby_home: Path | None = None) -> Path:
    return (gobby_home or default_gobby_home()) / "bootstrap.yaml"


def read_bootstrap_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as file:
        loaded = yaml.safe_load(file) or {}
    if not isinstance(loaded, dict):
        return {}
    return dict(loaded)


def write_bootstrap_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            yaml.safe_dump(data, file, default_flow_style=False, sort_keys=False)
            file.flush()
            os.fsync(file.fileno())
        tmp_path.chmod(0o600)
        os.replace(tmp_path, path)
        path.chmod(0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def update_bootstrap_yaml(
    path: Path,
    updater: Callable[[dict[str, Any]], None],
) -> None:
    data = read_bootstrap_yaml(path)
    updater(data)
    write_bootstrap_yaml(path, data)
