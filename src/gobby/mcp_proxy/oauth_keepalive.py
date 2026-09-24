"""Cross-process OAuth lock, shared backoff, and shape-only inspection.

A session advisory lock is held on its own connection. PostgreSQL releases that
lock when the connection closes, including process death. Backoff lives in the
stored OAuth state, so it is shared across processes and survives a restart.
"""

import asyncio
import contextvars
import hashlib
import logging
import math
import random
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import httpx2
import psycopg
from psycopg import sql

from gobby.mcp_proxy.models import MCPAuthorizationRequired, MCPServerConfig
from gobby.storage.secrets import SecretStore

logger = logging.getLogger(__name__)

ACCESS_REFRESH_LEAD_SECONDS = 600.0
KEEPALIVE_TICK_SECONDS = 60.0
_MAX_BACKOFF_SECONDS = 900.0
_SHAPE_FIELDS = ("tokens", "client", "expires_at", "metadata", "resource", "issuer")
_LOCK_HELD: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "gobby_oauth_lock_held", default=False
)
_TASKS: set[asyncio.Task[None]] = set()
_keepalive_tasks: dict[int, asyncio.Task[None]] = {}


def oauth_lock_key(name: str) -> int:
    """Stable signed 64-bit key for one stored OAuth identity."""
    digest = hashlib.sha256(name.encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def conninfo_for_store(store: object) -> str | None:
    """Return the hub DSN when this store is a real SecretStore."""
    database = getattr(store, "db", None)
    conninfo = getattr(database, "_conninfo", None)
    return conninfo if isinstance(conninfo, str) else None


def oauth_lock_is_held() -> bool:
    return _LOCK_HELD.get()


@contextmanager
def hold_oauth_lock() -> Iterator[None]:
    token = _LOCK_HELD.set(True)
    try:
        yield
    finally:
        _LOCK_HELD.reset(token)


def oauth_state_shape(state: object) -> dict[str, str | None]:
    """Presence and type names for the stored OAuth fields. Never values."""
    shape: dict[str, str | None] = {}
    for field in _SHAPE_FIELDS:
        value = getattr(state, field, None)
        shape[field] = None if value is None else type(value).__name__
    return shape


def backoff_active(state: object, now: float) -> bool:
    not_before = getattr(state, "retry_not_before", None)
    return isinstance(not_before, (int, float)) and now < float(not_before)


def schedule_backoff(state: Any, *, status: int, retry_after: str | None, now: float) -> None:
    """Record a shared retry time. Retry-After wins over the exponential delay."""
    del status
    attempt = int(getattr(state, "retry_attempt", 0) or 0) + 1
    delay = _retry_delay(attempt, retry_after, now)
    state.retry_attempt = attempt
    state.retry_not_before = now + delay


def token_refresh_due(expires_at: float | None, now: float) -> bool:
    """True when an access token expires inside the keep-alive lead."""
    if expires_at is None:
        return False
    return expires_at - now <= ACCESS_REFRESH_LEAD_SECONDS


def consent_required(payload: dict[str, Any]) -> bool:
    error = str(payload.get("error") or "")
    detail = f"{payload.get('error_description') or ''} {payload.get('message') or ''}"
    return error == "invalid_grant" or "revoked" in detail.casefold()


def _retry_delay(attempt: int, retry_after: str | None, now: float) -> float:
    honoured = _retry_after_seconds(retry_after, now)
    if honoured is not None:
        return honoured
    span = min(_MAX_BACKOFF_SECONDS, 5.0 * (2 ** (attempt - 1)))
    return float(span) * float(random.uniform(0.5, 1.0))


def _retry_after_seconds(retry_after: str | None, now: float) -> float | None:
    if retry_after is None or not str(retry_after).strip():
        return None
    text = str(retry_after).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime

    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    return max(0.0, float(moment.timestamp()) - now)


@asynccontextmanager
async def oauth_server_lock(
    name: str, conninfo: str, *, timeout_seconds: float = 30.0
) -> AsyncIterator[None]:
    """Serialize OAuth for one server across processes. Released on every exit."""
    key = oauth_lock_key(name)
    timeout_ms = max(1, math.ceil(timeout_seconds * 1000))
    connection = await psycopg.AsyncConnection.connect(
        conninfo,
        connect_timeout=max(1, math.ceil(timeout_seconds)),
        prepare_threshold=None,
    )
    locked = False
    try:
        await connection.execute(
            sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(timeout_ms))
        )
        await connection.execute("SELECT pg_advisory_lock(%s)", (key,))
        locked = True
        await connection.commit()
        yield
    finally:
        try:
            if not connection.closed:
                await connection.rollback()
                if locked:
                    await connection.execute("SELECT pg_advisory_unlock(%s)", (key,))
                    await connection.commit()
        finally:
            await connection.close()


def _refresh_ready(state: Any, now: float) -> bool:
    tokens = state.tokens
    if state.client is None or tokens is None or tokens.refresh_token is None:
        return False
    return not backoff_active(state, now) and token_refresh_due(state.expires_at, now)


async def refresh_server_if_due(
    config: MCPServerConfig,
    store: SecretStore,
    *,
    now: float,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> bool:
    """Refresh one due server under the cross-process lock, at the token endpoint."""
    from gobby.mcp_proxy.oauth import MCPOAuthStorage, PersistentOAuthProvider

    storage = MCPOAuthStorage(store, config)
    await storage.load()
    if not _refresh_ready(storage.state, now):
        return False
    conninfo = conninfo_for_store(store)
    if not isinstance(conninfo, str):
        return False
    before = storage.state.tokens.access_token if storage.state.tokens is not None else None
    async with oauth_server_lock(storage.name, conninfo):
        with hold_oauth_lock():
            await storage.load()
            if not _refresh_ready(storage.state, now):
                return False
            provider = PersistentOAuthProvider(config, storage)
            try:
                refreshed = await provider.refresh_persisted_token(transport=transport)
            except MCPAuthorizationRequired:
                raise
            except Exception:
                logger.warning("OAuth keep-alive refresh failed for %s", config.name)
                return False
    await storage.load()
    current = storage.state.tokens.access_token if storage.state.tokens is not None else None
    return bool(refreshed) and current is not None and current != before


async def refresh_due_oauth_servers(manager: object) -> None:
    database = getattr(manager, "mcp_db_manager", None)
    if database is None or not hasattr(database, "list_all_servers"):
        return
    rows = await asyncio.to_thread(database.list_all_servers, True)
    store = SecretStore(database.db)
    now = time.time()
    for row in rows:
        if not getattr(row, "requires_oauth", False) or not getattr(row, "url", None):
            continue
        if getattr(row, "transport", None) not in ("http", "sse"):
            continue
        config = MCPServerConfig(
            name=row.name,
            id=row.id,
            project_id=row.project_id,
            transport=row.transport,
            url=row.url,
            requires_oauth=True,
        )
        try:
            await refresh_server_if_due(config, store, now=now)
        except Exception:
            logger.warning("OAuth keep-alive skipped %s", getattr(row, "name", "server"))


async def oauth_keepalive_loop(manager: object) -> None:
    """Refresh authorized servers every KEEPALIVE_TICK_SECONDS. Never opens a browser."""
    while True:
        try:
            await refresh_due_oauth_servers(manager)
        except Exception:
            logger.exception("OAuth keep-alive pass failed")
        await asyncio.sleep(KEEPALIVE_TICK_SECONDS)


def _replace_keepalive_task(manager: object, loop: asyncio.AbstractEventLoop) -> None:
    key = id(manager)
    current = _keepalive_tasks.get(key)
    if current is not None and not current.done():
        return
    task = loop.create_task(oauth_keepalive_loop(manager), name="mcp-oauth-keepalive")
    _keepalive_tasks[key] = task
    _TASKS.add(task)

    def discard(done: asyncio.Task[None], owner: int = key) -> None:
        _TASKS.discard(done)
        if _keepalive_tasks.get(owner) is done:
            _keepalive_tasks.pop(owner, None)

    task.add_done_callback(discard)


def schedule_oauth_keepalive(
    manager: object, loop: asyncio.AbstractEventLoop | None = None
) -> None:
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
    loop.call_soon_threadsafe(_replace_keepalive_task, manager, loop)


def cancel_oauth_keepalive(manager: object, loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Cancel the keep-alive owned by this manager. Safe to call from a pool thread."""

    def stop() -> None:
        key = id(manager)
        task = _keepalive_tasks.get(key)
        if task is None:
            return
        if _keepalive_tasks.get(key) is task:
            _keepalive_tasks.pop(key, None)
        if not task.done():
            task.cancel()

    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            stop()
            return
    loop.call_soon_threadsafe(stop)
