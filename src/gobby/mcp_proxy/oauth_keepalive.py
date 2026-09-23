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
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import psycopg
from psycopg import sql

from gobby.mcp_proxy.models import MCPServerConfig
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


async def refresh_expired_token(
    storage: Any,
    *,
    now: float,
    refresh: Callable[[], Awaitable[None]],
    conninfo: str,
) -> bool:
    """Refresh once under the server lock. A waiter reloads the persisted token."""
    if backoff_active(storage.state, now):
        return False
    async with oauth_server_lock(storage.name, conninfo):
        await storage.load()
        expires_at = storage.state.expires_at
        if expires_at is not None and float(expires_at) > now:
            return False
        if backoff_active(storage.state, now):
            return False
        await refresh()
        await storage.save()
        return True


async def refresh_server_if_due(config: MCPServerConfig, store: SecretStore, *, now: float) -> bool:
    """Refresh one server when its access token is inside the lead window."""
    from gobby.mcp_proxy.oauth import MCPOAuthStorage, PersistentOAuthProvider

    storage = MCPOAuthStorage(store, config)
    await storage.load()
    state = storage.state
    if state.client is None or state.tokens is None or state.tokens.refresh_token is None:
        return False
    if backoff_active(state, now) or not token_refresh_due(state.expires_at, now):
        return False
    before = state.tokens.access_token
    provider = PersistentOAuthProvider(config, storage)
    await provider._initialize()
    provider.context.token_expiry_time = 0
    if config.url is None:
        return False
    import httpx2

    try:
        async with httpx2.AsyncClient(auth=provider, timeout=30) as client:
            await client.get(config.url)
    except Exception:
        logger.warning("OAuth keep-alive refresh failed for %s", config.name)
    await storage.load()
    current = storage.state.tokens.access_token if storage.state.tokens else None
    return current is not None and current != before


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
        headers = getattr(row, "headers", None)
        config = MCPServerConfig(
            name=row.name,
            id=row.id,
            project_id=row.project_id,
            transport=row.transport,
            url=row.url,
            requires_oauth=True,
            headers=store.resolve_dict(headers, project_id=row.project_id) if headers else None,
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


def schedule_oauth_keepalive(
    manager: object, loop: asyncio.AbstractEventLoop | None = None
) -> None:
    try:
        running = loop or asyncio.get_running_loop()
    except RuntimeError:
        return
    task = running.create_task(oauth_keepalive_loop(manager), name="mcp-oauth-keepalive")
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
