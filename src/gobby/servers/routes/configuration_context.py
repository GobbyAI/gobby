"""Shared dependencies for configuration route modules."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException

from gobby.config.app import DaemonConfig
from gobby.config.documents import ConfigDocumentsService
from gobby.config.runtime import ConfigRuntime
from gobby.config.values import ConfigValuesService
from gobby.prompts.loader import PromptLoader
from gobby.servers.routes._database import require_hub_database
from gobby.storage.config_mutations import ConfigMutations
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.config_store import ConfigStore
from gobby.storage.prompts import LocalPromptManager
from gobby.storage.secrets import SecretStore

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


class ConfigurationRouteContext:
    """Shared server-bound accessors for configuration route handlers."""

    def __init__(self, server: HTTPServer) -> None:
        self.server = server
        self._service_init_lock = Lock()

    def get_secret_store(self) -> SecretStore:
        return SecretStore(require_hub_database(self.server.services.database))

    def get_config_store(self) -> ConfigStore:
        store = getattr(self.server.services, "config_store", None)
        if isinstance(store, ConfigStore):
            return store
        with self._service_init_lock:
            store = getattr(self.server.services, "config_store", None)
            if not isinstance(store, ConfigStore):
                store = ConfigStore(require_hub_database(self.server.services.database))
                self.server.services.config_store = store
        return store

    def get_config_runtime(self) -> ConfigRuntime:
        runtime = getattr(self.server.services, "config_runtime", None)
        if not isinstance(runtime, ConfigRuntime):
            raise HTTPException(status_code=503, detail="Config runtime not available")
        return runtime

    def get_config_service(self) -> ConfigValuesService:
        service = getattr(self.server.services, "config_values_service", None)
        if isinstance(service, ConfigValuesService):
            return service
        with self._service_init_lock:
            service = getattr(self.server.services, "config_values_service", None)
            if not isinstance(service, ConfigValuesService):
                service = ConfigValuesService(
                    runtime=self.get_config_runtime(),
                    mutations=ConfigMutations(require_hub_database(self.server.services.database)),
                    run_blocking=self.run_config_db,
                )
                self.server.services.config_values_service = service
        return service

    def get_config_documents_service(self) -> ConfigDocumentsService:
        service = getattr(self.server.services, "config_documents_service", None)
        if isinstance(service, ConfigDocumentsService):
            return service
        with self._service_init_lock:
            service = getattr(self.server.services, "config_documents_service", None)
            if not isinstance(service, ConfigDocumentsService):
                database = require_hub_database(self.server.services.database)
                secret_store = SecretStore(database)
                repository = ConfigRepository(database, secret_store=secret_store)
                service = ConfigDocumentsService(
                    runtime=self.get_config_runtime(),
                    mutations=ConfigMutations(database, secret_store=secret_store),
                    runtime_candidate=repository.runtime_candidate,
                    resolve_secret=secret_store.get,
                    run_blocking=self.run_config_db,
                )
                self.server.services.config_documents_service = service
        return service

    async def run_config_db[T](self, operation: Callable[[], T]) -> T:
        return cast(T, await self.server.run_db(operation))

    def get_prompt_manager(self) -> LocalPromptManager:
        manager = getattr(self.server.services, "prompt_manager", None)
        if isinstance(manager, LocalPromptManager):
            return manager
        with self._service_init_lock:
            manager = getattr(self.server.services, "prompt_manager", None)
            if not isinstance(manager, LocalPromptManager):
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
        model_dump = getattr(config, "model_dump", None)
        if not callable(model_dump):
            raise HTTPException(status_code=503, detail="Config model not available")
        return cast(dict[str, Any], model_dump(mode="json", exclude_none=True, by_alias=True))

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
