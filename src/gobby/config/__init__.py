"""
Configuration package for Gobby daemon.

This package provides Pydantic config models for all daemon settings.
Configuration classes are organized into submodules by functionality:

Module structure:
- app.py: Main DaemonConfig aggregator and utility functions
- bootstrap.py: Pre-DB bootstrap settings from bootstrap.yaml
- ai.py: Daemon-owned AI generation configs
- local.py: Local model endpoint configs
- ui.py: Web UI, auth, and tool approval configs
- servers.py: WebSocket and MCP proxy configs
- persistence.py: Memory storage configs
- tasks.py: Task expansion, validation, and workflow configs
- extensions.py: Hook extension configs (webhooks, plugins)
- sessions.py: Session lifecycle and tracking configs
- features.py: MCP proxy feature configs (code execution, tool recommendation)

Import from submodules directly for specific configs:
    from gobby.config.ui import UIConfig
    from gobby.config.tasks import TaskValidationConfig
    from gobby.config.extensions import WebhooksConfig

Import from this package for app-level items:
    from gobby.config import BootstrapConfig, DaemonConfig, load_bootstrap
"""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.config.app import (
        DaemonConfig,
        expand_env_vars,
        export_config_to_yaml,
        load_yaml,
    )
    from gobby.config.bootstrap import BootstrapConfig, load_bootstrap
    from gobby.config.indexing import IndexingConfig

# Exports resolve on first access: importing a light submodule such as
# gobby.config.bootstrap runs this init, and app.py pulls in every config model
# plus telemetry. The stdio bridge must answer initialize without paying for it.
_EXPORT_MODULES = {
    "BootstrapConfig": "gobby.config.bootstrap",
    "DaemonConfig": "gobby.config.app",
    "IndexingConfig": "gobby.config.indexing",
    "expand_env_vars": "gobby.config.app",
    "export_config_to_yaml": "gobby.config.app",
    "load_bootstrap": "gobby.config.bootstrap",
    "load_yaml": "gobby.config.app",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    # Core app-level exports only
    "BootstrapConfig",
    "DaemonConfig",
    "IndexingConfig",
    "expand_env_vars",
    "export_config_to_yaml",
    "load_bootstrap",
    "load_yaml",
]
