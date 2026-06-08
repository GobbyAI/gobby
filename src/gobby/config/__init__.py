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
    from gobby.config.local import LocalConfig
    from gobby.config.ui import UIConfig
    from gobby.config.tasks import TaskValidationConfig
    from gobby.config.extensions import WebhooksConfig

Import from this package for app-level items:
    from gobby.config import DaemonConfig, load_config
"""

# Core configuration and utilities from app.py
from gobby.config.app import (
    DaemonConfig,
    expand_env_vars,
    export_config_to_yaml,
    load_config,
    load_yaml,
    save_config,
)
from gobby.config.indexing import IndexingConfig

__all__ = [
    # Core app-level exports only
    "DaemonConfig",
    "IndexingConfig",
    "expand_env_vars",
    "export_config_to_yaml",
    "load_config",
    "load_yaml",
    "save_config",  # deprecated alias
]
