"""Resolve canonical runtime values for managed-service Compose commands."""

from __future__ import annotations

import json
import os
import platform
from contextlib import ExitStack
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


class ComposeEnvironmentError(ValueError):
    """Raised when canonical managed-service configuration is unusable."""


@dataclass(frozen=True)
class ComposeRuntime:
    """Resolved subprocess environment and managed service profiles."""

    environment: dict[str, str]
    profiles: tuple[str, ...]


MANAGED_SERVICE_PROFILES = ("postgres", "qdrant", "falkordb")


def resolve_compose_runtime(
    gobby_home: Path,
    *,
    database_url: str | None = None,
    profiles: tuple[str, ...] = MANAGED_SERVICE_PROFILES,
    overrides: dict[str, str] | None = None,
) -> ComposeRuntime:
    """Resolve the environment required by the unified managed-services Compose file.

    Canonical values come from ``bootstrap.yaml``, ``config_store``, ``SecretStore``,
    and the bundled pg_search manifest. Process environment values override those
    values, while explicit caller overrides take final precedence.
    """
    home = gobby_home.expanduser()
    services_bind_address, files_home = _require_local_datastore_mode(home)
    canonical = _postgres_environment(home, database_url=database_url)
    canonical.update(_pgsearch_environment())
    canonical["GOBBY_SERVICES_BIND_ADDRESS"] = services_bind_address
    canonical["GOBBY_FILES_HOME"] = files_home

    unknown_profiles = set(profiles) - set(MANAGED_SERVICE_PROFILES)
    if unknown_profiles:
        raise ComposeEnvironmentError(
            f"Unknown managed-service profiles: {', '.join(sorted(unknown_profiles))}"
        )
    if "qdrant" in profiles or "falkordb" in profiles:
        service_values = _service_environment(home, required_profiles=profiles)
        canonical.update(service_values)

    environment = canonical | dict(os.environ)
    if overrides:
        environment.update(overrides)
    _validate_effective_environment(environment, profiles)
    return ComposeRuntime(environment=environment, profiles=profiles)


def _require_local_datastore_mode(gobby_home: Path) -> tuple[str, str]:
    from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

    bootstrap_path = gobby_home / "bootstrap.yaml"
    try:
        config = load_bootstrap(str(bootstrap_path))
    except BootstrapConfigError as exc:
        raise ComposeEnvironmentError(f"Invalid {bootstrap_path}: {exc}") from exc
    if config.datastore_mode == "remote":
        raise ComposeEnvironmentError(
            "this machine is in datastore_mode: remote; compose management runs on the hub"
        )
    if not config.files_home:
        raise ComposeEnvironmentError("files_home is not configured")
    return config.services_bind_address, config.files_home


def _postgres_environment(gobby_home: Path, *, database_url: str | None) -> dict[str, str]:
    dsn = database_url or _bootstrap_database_url(gobby_home)
    parsed = urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ComposeEnvironmentError(
            "bootstrap.yaml database_url must use postgresql:// for managed services"
        )

    try:
        port = parsed.port
    except ValueError as exc:
        raise ComposeEnvironmentError(
            f"bootstrap.yaml database_url has an invalid PostgreSQL port: {exc}"
        ) from exc

    database = unquote(parsed.path.lstrip("/"))
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    missing = [
        name
        for name, value in (
            ("username", username),
            ("password", password),
            ("port", port),
            ("database", database),
        )
        if not value
    ]
    if missing:
        fields = ", ".join(missing)
        raise ComposeEnvironmentError(
            f"bootstrap.yaml database_url is missing PostgreSQL {fields}; "
            "run `gobby postgres install` or repair the DSN"
        )

    return {
        "GOBBY_POSTGRES_DB": database,
        "GOBBY_POSTGRES_USER": username,
        "GOBBY_POSTGRES_PASSWORD": password,
        "GOBBY_POSTGRES_PORT": str(port),
    }


def _bootstrap_database_url(gobby_home: Path) -> str:
    from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

    bootstrap_path = gobby_home / "bootstrap.yaml"
    if not bootstrap_path.exists():
        raise ComposeEnvironmentError(
            f"Managed-service credentials are unavailable because {bootstrap_path} is missing; "
            "run `gobby postgres install` first"
        )
    try:
        config = load_bootstrap(str(bootstrap_path), resolve_database_url=True)
    except BootstrapConfigError as exc:
        raise ComposeEnvironmentError(f"Invalid {bootstrap_path}: {exc}") from exc
    if not config.database_url:
        raise ComposeEnvironmentError(
            f"{bootstrap_path} must define a PostgreSQL database_url for managed services"
        )
    return config.database_url


def _service_environment(
    gobby_home: Path,
    *,
    required_profiles: tuple[str, ...] = MANAGED_SERVICE_PROFILES,
) -> dict[str, str]:
    from gobby.storage.config_repository import ConfigRepository
    from gobby.storage.hub.runtime import runtime_hub_database
    from gobby.storage.secrets import SecretStore

    database_stack = ExitStack()
    try:
        db = database_stack.enter_context(
            runtime_hub_database(
                str(gobby_home / "bootstrap.yaml"),
                apply_migrations=False,
            )
        )
    except Exception as exc:
        raise ComposeEnvironmentError(
            f"Could not read managed-service config_store values: {exc}"
        ) from exc

    try:
        secret_store = SecretStore(db, gobby_home=gobby_home)
        snapshot = ConfigRepository(db, secret_store=secret_store).read(resolve_secrets=False)
        config_values = snapshot.values
        values: dict[str, str] = {}

        if "qdrant" in required_profiles:
            qdrant_url = config_values.get("databases.qdrant.url")
            if not isinstance(qdrant_url, str) or not qdrant_url.strip():
                raise ComposeEnvironmentError(
                    "Qdrant config is required; databases.qdrant.url must be set by `gobby install`"
                )
            qdrant_port = _positive_port(
                config_values.get("databases.qdrant.port"),
                "databases.qdrant.port",
            )
            values["GOBBY_QDRANT_HTTP_PORT"] = str(qdrant_port)
            values["GOBBY_QDRANT_GRPC_PORT"] = str(qdrant_port + 1)

        if "falkordb" in required_profiles:
            falkor_port = _positive_port(
                config_values.get("databases.falkordb.port"),
                "databases.falkordb.port",
            )
            falkor_host_value = config_values.get("databases.falkordb.host")
            if not isinstance(falkor_host_value, str) or not falkor_host_value.strip():
                raise ComposeEnvironmentError("databases.falkordb.host must be a non-empty string")
            falkor_host = falkor_host_value.strip()
            password_ref = config_values.get("databases.falkordb.password")
            if not isinstance(password_ref, str) or not password_ref.startswith("$secret:"):
                raise ComposeEnvironmentError(
                    "databases.falkordb.password must be a SecretStore reference"
                )
            secret_name = password_ref.removeprefix("$secret:")
            if not secret_name or not secret_store.exists(secret_name):
                raise ComposeEnvironmentError(
                    f"FalkorDB SecretStore entry {secret_name or '<empty>'!r} is missing"
                )
            password = secret_store.get(secret_name)
            if not password:
                raise ComposeEnvironmentError(
                    f"FalkorDB SecretStore entry {secret_name!r} is empty"
                )
            values["GOBBY_FALKORDB_HOST"] = falkor_host
            values["GOBBY_FALKORDB_PASSWORD"] = password
            values["GOBBY_FALKORDB_PORT"] = str(falkor_port)

        return values
    finally:
        database_stack.close()


def _positive_port(value: Any, key: str) -> int:
    if isinstance(value, bool):
        raise ComposeEnvironmentError(f"{key} must be a valid TCP port")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ComposeEnvironmentError(f"{key} must be a valid TCP port") from exc
    if not 1 <= port <= 65535:
        raise ComposeEnvironmentError(f"{key} must be between 1 and 65535")
    return port


def _validate_effective_environment(environment: dict[str, str], profiles: tuple[str, ...]) -> None:
    for key in (
        "GOBBY_POSTGRES_DB",
        "GOBBY_POSTGRES_USER",
        "GOBBY_POSTGRES_PASSWORD",
        "GOBBY_PG_SEARCH_VERSION",
        "GOBBY_PG_SEARCH_SHA256",
        "GOBBY_FILES_HOME",
    ):
        if not environment.get(key):
            raise ComposeEnvironmentError(f"{key} must not be empty")
    _positive_port(environment.get("GOBBY_POSTGRES_PORT"), "GOBBY_POSTGRES_PORT")

    if "qdrant" in profiles:
        _positive_port(environment.get("GOBBY_QDRANT_HTTP_PORT"), "GOBBY_QDRANT_HTTP_PORT")
        _positive_port(environment.get("GOBBY_QDRANT_GRPC_PORT"), "GOBBY_QDRANT_GRPC_PORT")
    if "falkordb" in profiles:
        _positive_port(environment.get("GOBBY_FALKORDB_PORT"), "GOBBY_FALKORDB_PORT")
        if not environment.get("GOBBY_FALKORDB_PASSWORD"):
            raise ComposeEnvironmentError("GOBBY_FALKORDB_PASSWORD must not be empty")


def _pgsearch_environment() -> dict[str, str]:
    manifest_ref = resources.files("gobby").joinpath("data/postgres-pgsearch/version.json")
    try:
        data = json.loads(manifest_ref.read_text(encoding="utf-8"))
        sha_by_arch = data.get("pg_search_sha256_by_arch", {})
        arch = _debian_arch(platform.machine())
        sha256 = (sha_by_arch.get(arch) if isinstance(sha_by_arch, dict) else None) or data[
            "pg_search_sha256"
        ]
        return {
            "GOBBY_PG_SEARCH_VERSION": str(data["pg_search_version"]),
            "GOBBY_PG_SEARCH_SHA256": str(sha256),
        }
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ComposeEnvironmentError(
            f"Bundled pg_search version manifest is invalid: {exc}"
        ) from exc


def _debian_arch(machine: str) -> str:
    normalized = machine.strip().lower()
    if normalized in {"arm64", "aarch64"}:
        return "arm64"
    if normalized in {"x86_64", "amd64"}:
        return "amd64"
    return normalized
