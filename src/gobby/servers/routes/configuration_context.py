"""Shared dependencies for configuration route modules."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import TYPE_CHECKING, cast

from fastapi import HTTPException

from gobby.config.documents import ConfigDocumentsService
from gobby.config.runtime import ConfigRuntime, ConfigSnapshot
from gobby.config.values import ConfigValuesError, ConfigValuesService
from gobby.prompts.loader import PromptLoader
from gobby.servers.routes._database import require_hub_database
from gobby.storage.config_mutations import ConfigMutations
from gobby.storage.config_repository import ConfigRepository
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

    def get_config_runtime(self) -> ConfigRuntime:
        runtime = getattr(self.server.services, "config_runtime", None)
        if not isinstance(runtime, ConfigRuntime):
            raise HTTPException(status_code=503, detail="Config runtime not available")
        return runtime

    def get_config_snapshot(self) -> ConfigSnapshot:
        """Return the current snapshot or a retryable startup error."""
        try:
            return self.get_config_runtime().snapshot
        except RuntimeError as exc:
            raise ConfigValuesError(
                "runtime_unavailable",
                "Configuration runtime is still starting",
                (),
                status_code=503,
                retryable=True,
            ) from exc

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
                    # ConfigDocumentsService restores scalar secret references to
                    # plaintext before this callback, so no bindings are required.
                    runtime_candidate=lambda overrides: repository.runtime_candidate(overrides, {}),
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
