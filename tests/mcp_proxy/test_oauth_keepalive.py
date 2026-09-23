"""Cross-process OAuth keep-alive: one refresh, backoff, and shape-only inspection."""

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import httpx2
import pytest
from mcp.client.auth import OAuthFlowError
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken
from pydantic import AnyUrl

from gobby.mcp_proxy.models import MCPAuthorizationRequired, MCPServerConfig
from gobby.mcp_proxy.oauth import MCPOAuthStorage, OAuthState, PersistentOAuthProvider
from gobby.mcp_proxy.oauth_keepalive import (
    ACCESS_REFRESH_LEAD_SECONDS,
    KEEPALIVE_TICK_SECONDS,
    backoff_active,
    oauth_lock_key,
    oauth_server_lock,
    oauth_state_shape,
    refresh_expired_token,
    schedule_backoff,
    token_refresh_due,
)
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit


def _store() -> SecretStore:
    values: dict[tuple[str, str], str] = {}
    store = MagicMock(spec=SecretStore)

    def get(name: str, *, project_id: str) -> str | None:
        return values.get((project_id, name))

    def put(name: str, value: str, *, project_id: str) -> None:
        values[project_id, name] = value

    store.get.side_effect = get
    store.set.side_effect = put
    return store


def _config(name: str = "fieldy") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        project_id=GLOBAL_PROJECT_ID,
        url=f"https://{name}.example/mcp",
        transport="http",
    )


def _conninfo() -> str:
    return os.environ["DATABASE_URL"]


def test_oauth_state_shape_reports_types_and_hides_credentials() -> None:
    secret = "secret-token-value"
    refresh = "refresh-secret-value"
    client_secret = "super-secret-value"
    state = OAuthState(
        tokens=OAuthToken(access_token=secret, token_type="Bearer", refresh_token=refresh),
        client=OAuthClientInformationFull(
            client_id="client-id",
            client_secret=client_secret,
            redirect_uris=[AnyUrl("http://127.0.0.1:9/callback")],
        ),
        expires_at=1_700_000_000.0,
        issuer="https://api.fieldy.ai/api/auth",
    )
    shape = oauth_state_shape(state)
    visible = str(shape)
    assert shape["tokens"] == "OAuthToken"
    assert shape["client"] == "OAuthClientInformationFull"
    assert shape["expires_at"] == "float"
    assert shape["metadata"] is None
    assert shape["resource"] is None
    assert shape["issuer"] == "str"
    assert secret not in visible
    assert refresh not in visible
    assert client_secret not in visible


def test_retry_after_backoff_blocks_a_second_call_inside_the_window() -> None:
    state = OAuthState()
    schedule_backoff(state, status=429, retry_after="30", now=1_000.0)
    assert backoff_active(state, 1_010.0)
    assert not backoff_active(state, 1_031.0)
    assert state.retry_not_before == 1_030.0


def test_idle_token_is_due_inside_the_named_lead() -> None:
    now = 5_000.0
    assert token_refresh_due(now + ACCESS_REFRESH_LEAD_SECONDS, now)
    assert not token_refresh_due(now + ACCESS_REFRESH_LEAD_SECONDS + 1, now)
    assert KEEPALIVE_TICK_SECONDS == 60


@pytest.mark.asyncio
async def test_two_authorizations_for_one_server_serialize() -> None:
    name = "same-server"
    held = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        async with oauth_server_lock(name, _conninfo(), timeout_seconds=5):
            held.set()
            await release.wait()

    async def second() -> None:
        await held.wait()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(2):
                async with oauth_server_lock(name, _conninfo(), timeout_seconds=5):
                    raise AssertionError(
                        "second authorization entered while the first held the lock"
                    )
        release.set()
        async with oauth_server_lock(name, _conninfo(), timeout_seconds=5):
            return

    await asyncio.wait_for(asyncio.gather(first(), second()), timeout=8)


@pytest.mark.asyncio
async def test_authorizations_for_different_servers_overlap() -> None:
    ready = 0
    release = asyncio.Event()

    async def hold(name: str) -> None:
        nonlocal ready
        async with oauth_server_lock(name, _conninfo(), timeout_seconds=5):
            ready += 1
            if ready < 2:
                await asyncio.wait_for(release.wait(), timeout=2)
            else:
                release.set()

    await asyncio.wait_for(asyncio.gather(hold("server-a"), hold("server-b")), timeout=3)
    assert ready == 2


@pytest.mark.asyncio
async def test_oauth_lock_releases_on_exception() -> None:
    name = "exception-server"

    with pytest.raises(RuntimeError, match="boom"):
        async with oauth_server_lock(name, _conninfo(), timeout_seconds=5):
            raise RuntimeError("boom")

    async with asyncio.timeout(2):
        async with oauth_server_lock(name, _conninfo(), timeout_seconds=2):
            return


@pytest.mark.asyncio
async def test_concurrent_expired_token_calls_refresh_once() -> None:
    store = _store()
    config = _config("refresh-once")
    storage = MCPOAuthStorage(store, config)
    storage.state.tokens = OAuthToken(
        access_token="old-access", token_type="Bearer", refresh_token="old-refresh"
    )
    storage.state.expires_at = 1.0
    await storage.save()
    calls = 0
    inside = asyncio.Event()
    release = asyncio.Event()

    async def refresh() -> None:
        nonlocal calls
        calls += 1
        inside.set()
        await release.wait()
        storage.state.tokens = OAuthToken(
            access_token="new-access", token_type="Bearer", refresh_token="new-refresh"
        )
        storage.state.expires_at = time.time() + 3600

    async def once() -> bool:
        return await refresh_expired_token(
            storage, now=100.0, refresh=refresh, conninfo=_conninfo()
        )

    first = asyncio.create_task(once())
    await inside.wait()
    second = asyncio.create_task(once())
    turned = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(turned.set_result, None)
    await turned
    assert calls == 1
    release.set()
    did_first, did_second = await asyncio.gather(first, second)
    assert did_first is True
    assert did_second is False


@pytest.mark.asyncio
async def test_reloaded_storage_authorizes_without_registration() -> None:
    store = _store()
    config = _config("restart")
    first = MCPOAuthStorage(store, config)
    first.state.tokens = OAuthToken(
        access_token="kept-access", token_type="Bearer", refresh_token="kept-refresh"
    )
    first.state.client = OAuthClientInformationFull(
        client_id="kept-client",
        redirect_uris=[AnyUrl("http://127.0.0.1:9/callback")],
    )
    first.state.expires_at = time.time() + 3600
    await first.save()

    posts: list[str] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            posts.append(str(request.url))
            return httpx2.Response(500, json={"error": "should-not-register"})
        assert request.headers["Authorization"] == "Bearer kept-access"
        return httpx2.Response(200, json={"ok": True})

    second = MCPOAuthStorage(store, config)
    provider = PersistentOAuthProvider(config, second)
    async with httpx2.AsyncClient(auth=provider, transport=httpx2.MockTransport(respond)) as client:
        response = await client.get(config.url or "")
    assert response.status_code == 200
    assert posts == []


@pytest.mark.asyncio
async def test_backoff_second_call_makes_no_token_request() -> None:
    store = _store()
    config = _config("backoff")
    storage = MCPOAuthStorage(store, config)
    storage.state.tokens = OAuthToken(
        access_token="expired-access", token_type="Bearer", refresh_token="expired-refresh"
    )
    storage.state.client = OAuthClientInformationFull(
        client_id="client",
        redirect_uris=[AnyUrl("http://127.0.0.1:9/callback")],
    )
    storage.state.expires_at = 1.0
    schedule_backoff(storage.state, status=429, retry_after="30", now=time.time())
    await storage.save()
    provider = PersistentOAuthProvider(config, storage)
    await provider._initialize()
    flow = provider.async_auth_flow(httpx2.Request("GET", config.url or ""))
    with pytest.raises(OAuthFlowError, match="backoff"):
        await anext(flow)


@pytest.mark.asyncio
async def test_invalid_grant_surfaces_authorization_required_once() -> None:
    store = _store()
    config = _config("consent")
    storage = MCPOAuthStorage(store, config)
    storage.state.tokens = OAuthToken(
        access_token="dead-access", token_type="Bearer", refresh_token="dead-refresh"
    )
    storage.state.client = OAuthClientInformationFull(
        client_id="client",
        redirect_uris=[AnyUrl("http://127.0.0.1:9/callback")],
    )
    storage.state.expires_at = 1.0
    await storage.save()
    calls = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if request.method == "POST":
            return httpx2.Response(
                400, json={"error": "invalid_grant", "error_description": "revoked"}
            )
        return httpx2.Response(401)

    provider = PersistentOAuthProvider(config, storage)
    with pytest.raises(MCPAuthorizationRequired, match="mcp-proxy auth"):
        async with httpx2.AsyncClient(
            auth=provider, transport=httpx2.MockTransport(respond)
        ) as client:
            await client.get(config.url or "")
    assert calls == 1


def _metadata_response() -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "issuer": "https://auth.example",
            "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": "https://auth.example/token",
            "registration_endpoint": "https://auth.example/register",
            "response_types_supported": ["code"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": True,
        },
    )


def _registration_transport(posts: list[str]) -> httpx2.MockTransport:
    def respond(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.method == "POST":
            posts.append(path)
        if path == "/mcp":
            return httpx2.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://resource.example/prm"'
                },
            )
        if path == "/prm":
            return httpx2.Response(
                200,
                json={
                    "resource": "https://resource.example",
                    "authorization_servers": ["https://auth.example"],
                    "scopes_supported": ["conversations:read"],
                },
            )
        if "oauth-authorization-server" in path or path.endswith("openid-configuration"):
            return _metadata_response()
        if path == "/register":
            return httpx2.Response(429, headers={"Retry-After": "30"}, json={"error": "slow_down"})
        return httpx2.Response(404)

    return httpx2.MockTransport(respond)


@pytest.mark.asyncio
async def test_registration_429_persists_shared_backoff(
    postgres_db: PostgresHubDatabase, tmp_path: Path
) -> None:
    """A registration 429 is shared through reloaded storage and blocks the next call."""
    config = _config("register-backoff")
    config.url = "https://resource.example/mcp"
    store = SecretStore(postgres_db, gobby_home=tmp_path)
    existing = MCPOAuthStorage(store, config)
    await existing.save()
    posts: list[str] = []

    async def redirect(_url: str) -> None:
        raise AssertionError("registration backoff must not open a browser")

    provider = PersistentOAuthProvider(config, existing, redirect_handler=redirect)
    with pytest.raises(OAuthFlowError):
        async with httpx2.AsyncClient(
            auth=provider, transport=_registration_transport(posts)
        ) as client:
            await client.get(config.url)
    assert posts == ["/register"]

    reloaded = MCPOAuthStorage(SecretStore(postgres_db, gobby_home=tmp_path), config)
    await reloaded.load()
    retry_not_before = reloaded.state.retry_not_before
    assert retry_not_before is not None
    assert retry_not_before > time.time()

    second_posts: list[str] = []
    fresh = PersistentOAuthProvider(
        config,
        MCPOAuthStorage(SecretStore(postgres_db, gobby_home=tmp_path), config),
        redirect_handler=redirect,
    )
    with pytest.raises(OAuthFlowError, match="backoff"):
        async with httpx2.AsyncClient(
            auth=fresh, transport=_registration_transport(second_posts)
        ) as client:
            await client.get(config.url)
    assert second_posts == []


def _advisory_lock_ids(key: int) -> tuple[int, int]:
    raw = key & ((1 << 64) - 1)
    return (raw >> 32) & 0xFFFFFFFF, raw & 0xFFFFFFFF


async def _refresh_waiter_blocked(db: PostgresHubDatabase, name: str) -> bool:
    classid, objid = _advisory_lock_ids(oauth_lock_key(name))
    query = """SELECT COUNT(*) AS waiting
               FROM pg_locks
               WHERE locktype = 'advisory'
                 AND classid = %s
                 AND objid = %s
                 AND objsubid = 1
                 AND NOT granted"""
    for _ in range(200):
        row = await asyncio.to_thread(db.fetchone, query, (classid, objid))
        if row is not None and int(row["waiting"]) >= 1:
            return True
    return False


@pytest.mark.asyncio
async def test_async_auth_flow_refreshes_once_and_reuses_persisted_tokens(
    postgres_db: PostgresHubDatabase, tmp_path: Path
) -> None:
    """Two providers refresh through one token POST, then the waiter uses the saved tokens."""
    config = _config("refresh-flight")
    seeded = MCPOAuthStorage(SecretStore(postgres_db, gobby_home=tmp_path), config)
    seeded.state.tokens = OAuthToken(
        access_token="old-access", token_type="Bearer", refresh_token="old-refresh"
    )
    seeded.state.client = OAuthClientInformationFull(
        client_id="client",
        redirect_uris=[AnyUrl("http://127.0.0.1:9/callback")],
    )
    seeded.state.metadata = OAuthMetadata(
        issuer=AnyUrl("https://auth.example"),
        authorization_endpoint=AnyUrl("https://auth.example/authorize"),
        token_endpoint=AnyUrl("https://auth.example/token"),
        response_types_supported=["code"],
    )
    seeded.state.expires_at = 1.0
    await seeded.save()
    posts: list[str] = []
    auths: list[str | None] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def respond(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            posts.append(request.url.path)
            if len(posts) == 1:
                started.set()
                await release.wait()
            return httpx2.Response(
                200,
                json={
                    "access_token": "rotated-access",
                    "token_type": "Bearer",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 3600,
                },
            )
        auths.append(request.headers.get("Authorization"))
        return httpx2.Response(200, json={"ok": True})

    def provider() -> PersistentOAuthProvider:
        storage = MCPOAuthStorage(SecretStore(postgres_db, gobby_home=tmp_path), config)
        return PersistentOAuthProvider(config, storage)

    async def drive(auth: PersistentOAuthProvider) -> None:
        async with httpx2.AsyncClient(auth=auth, transport=httpx2.MockTransport(respond)) as client:
            response = await client.get(config.url or "")
        assert response.status_code == 200

    async with asyncio.TaskGroup() as group:
        group.create_task(drive(provider()))
        await started.wait()
        group.create_task(drive(provider()))
        assert await _refresh_waiter_blocked(postgres_db, seeded.name)
        midpoint = MCPOAuthStorage(SecretStore(postgres_db, gobby_home=tmp_path), config)
        await midpoint.load()
        assert midpoint.state.tokens is not None
        assert midpoint.state.tokens.access_token == "old-access"
        release.set()

    assert posts == ["/token"]
    assert auths == ["Bearer rotated-access", "Bearer rotated-access"]
    finished = MCPOAuthStorage(SecretStore(postgres_db, gobby_home=tmp_path), config)
    await finished.load()
    assert finished.state.tokens is not None
    assert finished.state.tokens.access_token == "rotated-access"
    assert finished.state.tokens.refresh_token == "rotated-refresh"
