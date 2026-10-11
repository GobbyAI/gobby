"""Managed datastore services used by daemon and offline CLI operations."""

from pathlib import Path

from ._daemon_services import ServiceStartResult, start_managed_services, stop_managed_services
from .installers.compose_env import resolve_compose_runtime


def _services_start(gobby_home: Path, *, require_schema_owner: bool = False) -> ServiceStartResult:
    return start_managed_services(
        gobby_home,
        resolve_runtime=resolve_compose_runtime,
        require_schema_owner=require_schema_owner,
    )


def _services_stop(gobby_home: Path) -> bool:
    return stop_managed_services(gobby_home, resolve_runtime=resolve_compose_runtime)
