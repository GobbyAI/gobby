"""Shared dependencies for configuration route modules."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException

from gobby.config.app import DaemonConfig
from gobby.prompts.loader import PromptLoader
from gobby.servers.routes._database import require_hub_database
from gobby.storage.config_store import ConfigStore
from gobby.storage.prompts import LocalPromptManager
from gobby.storage.secrets import SecretStore

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


class ConfigurationRouteContext:
    """Shared server-bound accessors for configuration route handlers."""

    def __init__(self, server: HTTPServer) -> None:
        self.server = server

    def get_secret_store(self) -> SecretStore:
        return SecretStore(require_hub_database(self.server.services.database))

    def get_config_store(self) -> ConfigStore:
        store = getattr(self.server.services, "config_store", None)
        if not isinstance(store, ConfigStore):
            store = ConfigStore(require_hub_database(self.server.services.database))
            self.server.services.config_store = store
        return store

    def get_prompt_manager(self) -> LocalPromptManager:
        manager = getattr(self.server.services, "prompt_manager", None)
        if isinstance(manager, LocalPromptManager):
            return manager
        dev_mode = getattr(self.server.services, "dev_mode", False)
        manager = LocalPromptManager(self.server.services.database, dev_mode=dev_mode)
        self.server.services.prompt_manager = manager
        return manager

    def get_prompt_loader(self) -> PromptLoader:
        return PromptLoader(
            db=self.server.services.database,
            project_id=self.server.services.project_id,
        )

    def current_config_values(self) -> dict[str, Any]:
        config = getattr(self.server.services, "config", None)
        if config is None:
            raise HTTPException(status_code=503, detail="Config not available")
        if not hasattr(config, "model_dump"):
            raise HTTPException(status_code=503, detail="Config model not available")
        return cast(dict[str, Any], config.model_dump(mode="json", exclude_none=True))

    def set_runtime_config(
        self,
        config: DaemonConfig,
        *,
        propagate_websocket: bool = False,
    ) -> None:
        self.server.services.config = config
        if not propagate_websocket:
            return

        ws_server = getattr(self.server.services, "websocket_server", None)
        if ws_server is not None and hasattr(ws_server, "daemon_config"):
            ws_server.daemon_config = config
