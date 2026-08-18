"""Reachability checks for clients using hub-managed datastores."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import httpx
import psycopg
from psycopg.rows import dict_row
from qdrant_client import AsyncQdrantClient

from gobby.paths import get_gobby_home
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SECRET_KEY_ID, SecretStore

CONNECT_TIMEOUT_SECONDS = 3
OPERATION_TIMEOUT_SECONDS = 5
OVERALL_TIMEOUT_SECONDS = 15
FILES_PROXY_HOP_HEADER = "X-Gobby-Files-Proxy-Hop"
USER_MD_PROBE_PATH = "/api/files/user-md"

_CONFIG_KEYS = (
    "databases.qdrant.url",
    "databases.falkordb.host",
    "databases.falkordb.port",
    "databases.falkordb.password",
)


class _AsyncCursor(Protocol):
    async def fetchone(self) -> Mapping[str, Any] | None: ...

    async def fetchall(self) -> list[Mapping[str, Any]]: ...


class _AsyncPostgres(Protocol):
    async def execute(
        self,
        query: str,
        params: object = (),
    ) -> _AsyncCursor: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class RemoteDatastoreConfig:
    """Remote service endpoints resolved from the shared ConfigStore."""

    qdrant_url: str
    falkordb_host: str
    falkordb_port: int
    falkordb_password: str


class RemotePreflightError(RuntimeError):
    """One bounded remote service probe failed."""

    def __init__(self, service: str, phase: str, detail: str) -> None:
        self.service = service
        self.phase = phase
        super().__init__(f"{service} remote preflight failed during {phase}: {detail}")


class _PrefetchedSecretDatabase:
    """Expose prefetched secret rows to SecretStore without another connection."""

    def __init__(
        self,
        key_material: Mapping[str, Any],
        secret: Mapping[str, Any],
    ) -> None:
        self._key_material = key_material
        self._secret = secret

    def fetchone(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Mapping[str, Any] | None:
        del params
        if "FROM secret_key_material" in sql:
            return self._key_material
        if "FROM secrets" in sql:
            return self._secret
        raise RuntimeError("Unexpected query while resolving remote FalkorDB secret")


async def _bounded[T](
    service: str,
    phase: str,
    timeout_seconds: float,
    operation: Awaitable[T],
) -> T:
    try:
        async with asyncio.timeout(timeout_seconds):
            return await operation
    except TimeoutError as exc:
        raise RemotePreflightError(
            service,
            phase,
            f"timed out after {timeout_seconds:g}s",
        ) from exc
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise RemotePreflightError(service, phase, f"{type(exc).__name__}: {exc}") from exc


async def _close_client(client: object, method_name: str) -> None:
    close = getattr(client, method_name, None)
    if not callable(close):
        return
    try:
        result = close()
        if not inspect.isawaitable(result):
            return
        task = asyncio.ensure_future(result)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
    except asyncio.CancelledError:
        raise
    except Exception:
        return


async def _connect_postgres(database_url: str) -> _AsyncPostgres:
    return cast(
        _AsyncPostgres,
        await psycopg.AsyncConnection.connect(
            database_url,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            row_factory=dict_row,
        ),
    )


def _decode_config_value(key: str, value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise RemotePreflightError("PostgreSQL", "config_store read", f"invalid {key}") from exc


def _require_string(values: Mapping[str, object], key: str, service: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RemotePreflightError(service, "config_store read", f"missing {key}")
    return value


async def _fetchone(
    connection: _AsyncPostgres,
    service: str,
    phase: str,
    query: str,
    params: object,
) -> Mapping[str, Any] | None:
    cursor = await _bounded(
        service,
        phase,
        OPERATION_TIMEOUT_SECONDS,
        connection.execute(query, params),
    )
    return await _bounded(
        service,
        phase,
        OPERATION_TIMEOUT_SECONDS,
        cursor.fetchone(),
    )


async def _read_remote_config(
    connection: _AsyncPostgres,
    gobby_home: Path,
) -> RemoteDatastoreConfig:
    cursor = await _bounded(
        "PostgreSQL",
        "config_store read",
        OPERATION_TIMEOUT_SECONDS,
        connection.execute(
            "SELECT key, value FROM config_store WHERE key = ANY(%s)",
            (list(_CONFIG_KEYS),),
        ),
    )
    rows = await _bounded(
        "PostgreSQL",
        "config_store read",
        OPERATION_TIMEOUT_SECONDS,
        cursor.fetchall(),
    )
    values = {str(row["key"]): _decode_config_value(str(row["key"]), row["value"]) for row in rows}
    qdrant_url = _require_string(values, "databases.qdrant.url", "Qdrant")
    falkordb_host = _require_string(values, "databases.falkordb.host", "FalkorDB")
    port_value = values.get("databases.falkordb.port")
    if (
        isinstance(port_value, bool)
        or not isinstance(port_value, int)
        or not 1 <= port_value <= 65535
    ):
        raise RemotePreflightError(
            "FalkorDB",
            "config_store read",
            "missing or invalid databases.falkordb.port",
        )
    password_ref = _require_string(values, "databases.falkordb.password", "FalkorDB")
    if not password_ref.startswith("$secret:") or not password_ref.removeprefix("$secret:"):
        raise RemotePreflightError(
            "FalkorDB",
            "config_store read",
            "databases.falkordb.password must be a $secret: reference",
        )
    secret_name = password_ref.removeprefix("$secret:").lower()
    key_material = await _fetchone(
        connection,
        "FalkorDB",
        "secret read",
        """SELECT id, wrapped_dek, kek_posture, kek_salt, kek_kdf_n, kek_kdf_r, kek_kdf_p
           FROM secret_key_material WHERE id = %s""",
        (SECRET_KEY_ID,),
    )
    secret = await _fetchone(
        connection,
        "FalkorDB",
        "secret read",
        "SELECT encrypted_value FROM secrets WHERE name = %s",
        (secret_name,),
    )
    if key_material is None or secret is None:
        raise RemotePreflightError(
            "FalkorDB",
            "secret read",
            "shared FalkorDB secret is missing",
        )
    database = cast(HubDatabase, _PrefetchedSecretDatabase(key_material, secret))
    try:
        password = SecretStore(database, gobby_home=gobby_home).get(secret_name)
    except Exception as exc:
        raise RemotePreflightError(
            "FalkorDB",
            "secret unwrap",
            f"{type(exc).__name__}: {exc}",
        ) from exc
    if not password:
        raise RemotePreflightError("FalkorDB", "secret read", "shared password is empty")
    return RemoteDatastoreConfig(
        qdrant_url=qdrant_url,
        falkordb_host=falkordb_host,
        falkordb_port=port_value,
        falkordb_password=password,
    )


async def _probe_postgres(
    database_url: str,
    gobby_home: Path,
) -> RemoteDatastoreConfig:
    connection: _AsyncPostgres | None = None
    try:
        connection = await _bounded(
            "PostgreSQL",
            "connect",
            CONNECT_TIMEOUT_SECONDS,
            _connect_postgres(database_url),
        )
        await _bounded(
            "PostgreSQL",
            "query",
            OPERATION_TIMEOUT_SECONDS,
            connection.execute("SELECT 1"),
        )
        return await _read_remote_config(connection, gobby_home)
    finally:
        if connection is not None:
            await _close_client(connection, "close")


def _create_qdrant_client(url: str) -> AsyncQdrantClient:
    return AsyncQdrantClient(
        url=url,
        timeout=OPERATION_TIMEOUT_SECONDS,
        check_compatibility=False,
    )


async def _probe_qdrant(url: str) -> None:
    try:
        client = _create_qdrant_client(url)
    except Exception as exc:
        raise RemotePreflightError(
            "Qdrant",
            "client setup",
            f"{type(exc).__name__}: {exc}",
        ) from exc
    try:
        await _bounded(
            "Qdrant",
            "health",
            OPERATION_TIMEOUT_SECONDS,
            client.get_collections(),
        )
    finally:
        await _close_client(client, "close")


def _create_falkordb_client(config: RemoteDatastoreConfig) -> Any:
    redis = importlib.import_module("redis.asyncio")
    retry_module = importlib.import_module("redis.retry")
    backoff_module = importlib.import_module("redis.backoff")
    return redis.Redis(
        host=config.falkordb_host,
        port=config.falkordb_port,
        password=config.falkordb_password,
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=OPERATION_TIMEOUT_SECONDS,
        retry=retry_module.Retry(backoff_module.NoBackoff(), 0),
        health_check_interval=0,
        single_connection_client=True,
    )


async def _probe_falkordb(config: RemoteDatastoreConfig) -> None:
    try:
        client = _create_falkordb_client(config)
    except Exception as exc:
        raise RemotePreflightError(
            "FalkorDB",
            "client setup",
            f"{type(exc).__name__}: {exc}",
        ) from exc
    try:
        result = await _bounded(
            "FalkorDB",
            "PING",
            OPERATION_TIMEOUT_SECONDS,
            client.ping(),
        )
        if result is not True:
            raise RemotePreflightError("FalkorDB", "PING", f"unexpected response {result!r}")
    finally:
        await _close_client(client, "aclose")


def _actionable_error(error: RemotePreflightError) -> str:
    guidance = {
        "PostgreSQL": "Check bootstrap.yaml database_url and Tailscale ACLs.",
        "Qdrant": "Run `gobby datastores expose` on the hub and verify Tailscale ACLs.",
        "FalkorDB": (
            "Run `gobby datastores expose` on the hub, verify Tailscale ACLs, and copy "
            "the hub ~/.gobby/.secret_kek so the shared $secret: password can be unwrapped."
        ),
    }
    return f"{error}. {guidance[error.service]}"


def probe_hub_user_md(hub_daemon_url: str, *, gobby_home: Path) -> list[str]:
    """Authenticated owner probe. Success is 200 JSON from a local files owner."""
    token_path = gobby_home / "local_cli_token"
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        return [
            f"Remote datastore install requires {token_path}. Copy it from the hub to "
            f"{token_path} to authenticate to the shared hub; the client installer "
            "will never generate or rotate it."
        ]
    origin = hub_daemon_url.rstrip("/")
    url = f"{origin}{USER_MD_PROBE_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        FILES_PROXY_HOP_HEADER: "1",
    }
    try:
        response = httpx.get(
            url,
            headers=headers,
            timeout=OPERATION_TIMEOUT_SECONDS,
            trust_env=False,
        )
    except httpx.TimeoutException:
        return ["Hub USER.md probe timed out. Check hub_daemon_url reachability and retry."]
    except httpx.HTTPError as exc:
        return [f"Hub USER.md probe network failure: {type(exc).__name__}: {exc}"]

    if response.status_code in {401, 403}:
        return [
            "Hub USER.md probe authentication failed. Copy the hub local_cli_token "
            "and retry; the installer does not generate or rotate it."
        ]
    if response.status_code == 404:
        return ["Hub USER.md probe found no owner files_home root."]
    if response.status_code in {400, 409, 421}:
        detail = _probe_error_code(response)
        if detail == "hop_refused":
            return ["Hub USER.md probe hop refused."]
        if detail == "remote_target":
            return ["Hub USER.md probe targeted a remote daemon, not the files owner."]
        return [f"Hub USER.md probe refused ({response.status_code})."]
    if response.status_code != 200:
        return [f"Hub USER.md probe failed with HTTP {response.status_code}."]
    try:
        payload = response.json()
    except ValueError:
        return ["Hub USER.md probe did not return JSON."]
    if not isinstance(payload, dict):
        return ["Hub USER.md probe did not return owner profile JSON."]
    if payload.get("owner") == "remote" or payload.get("files_owner") is False:
        return ["Hub USER.md probe targeted a remote daemon, not the files owner."]
    if not isinstance(payload.get("content"), str):
        return ["Hub USER.md probe did not return owner profile JSON."]
    return []


def _probe_error_code(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, str) and error:
            return error
    text = response.text.lower()
    if "hop" in text:
        return "hop_refused"
    if "remote" in text:
        return "remote_target"
    return ""


def _credential_errors(gobby_home: Path) -> list[str]:
    errors: list[str] = []
    for filename, purpose in (
        (".secret_kek", "unwrap shared FalkorDB secrets"),
        ("local_cli_token", "authenticate to the shared hub"),
    ):
        path = gobby_home / filename
        if not path.is_file():
            errors.append(
                f"Remote datastore install requires {path}. Copy it from the hub to {path} "
                f"to {purpose}; the client installer will never generate or rotate it."
            )
    return errors


async def _run_remote_preflight_async(
    database_url: str,
    gobby_home: Path,
) -> list[str]:
    try:
        async with asyncio.timeout(OVERALL_TIMEOUT_SECONDS):
            try:
                config = await _probe_postgres(database_url, gobby_home)
            except RemotePreflightError as exc:
                return [_actionable_error(exc)]
            results = await asyncio.gather(
                _probe_qdrant(config.qdrant_url),
                _probe_falkordb(config),
                return_exceptions=True,
            )
    except TimeoutError:
        return [
            "Remote datastore preflight timed out during the 15s overall deadline. "
            "Check PostgreSQL query, Qdrant health, and FalkorDB PING reachability."
        ]
    errors: list[str] = []
    for result in results:
        if isinstance(result, RemotePreflightError):
            errors.append(_actionable_error(result))
        elif isinstance(result, BaseException):
            errors.append(f"Remote datastore preflight failed: {type(result).__name__}: {result}")
    return errors


def run_remote_preflight(
    database_url: str,
    *,
    gobby_home: Path | None = None,
    hub_daemon_url: str | None = None,
) -> list[str]:
    """Run one zero-retry remote reachability pass and return actionable errors."""
    home = (gobby_home or get_gobby_home()).expanduser()
    if errors := _credential_errors(home):
        return errors
    if hub_daemon_url:
        if errors := probe_hub_user_md(hub_daemon_url, gobby_home=home):
            return errors
    return asyncio.run(_run_remote_preflight_async(database_url, home))
