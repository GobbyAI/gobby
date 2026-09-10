"""OAuth wire-flow and persistence tests against an isolated authorization server."""

import base64
import hashlib
import json
import time
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import click
import httpx2
import pytest
from mcp.client.auth import AuthorizationCodeResult, OAuthFlowError

from gobby.cli.mcp_oauth import authorize_server
from gobby.mcp_proxy.models import MCPAuthorizationRequired, MCPServerConfig
from gobby.mcp_proxy.oauth import MCPOAuthStorage, PersistentOAuthProvider
from gobby.mcp_proxy.transports.factory import create_transport_connection
from gobby.mcp_proxy.transports.http import HTTPTransportConnection
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit


@pytest.fixture
def secret_store() -> SecretStore:
    values: dict[tuple[str, str], str] = {}
    store = MagicMock(spec=SecretStore)

    def get(name: str, *, project_id: str) -> str | None:
        return values.get((project_id, name))

    def put(name: str, value: str, *, project_id: str) -> None:
        values[project_id, name] = value

    store.get.side_effect = get
    store.set.side_effect = put
    return store


class AuthorizationServer:
    def __init__(self) -> None:
        self.authorization: dict[str, list[str]] = {}
        self.registration: dict[str, Any] = {}
        self.refreshes = 0
        self.deny_refresh = False
        self.requests: list[httpx2.Request] = []

    async def redirect(self, url: str) -> None:
        self.authorization = parse_qs(urlsplit(url).query)

    async def callback(self) -> AuthorizationCodeResult:
        return AuthorizationCodeResult(
            code="consent-code", state=self.authorization["state"][0], iss="https://auth.example"
        )

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/mcp":
            if request.headers.get("Authorization") in ("Bearer access", "Bearer refreshed"):
                return httpx2.Response(200, json={"authenticated": True})
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
                    "scopes_supported": ["conversations:read", "offline_access"],
                },
            )
        if path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(
                200,
                json={
                    "issuer": "https://auth.example",
                    "authorization_endpoint": "https://auth.example/authorize",
                    "token_endpoint": "https://auth.example/custom/token",
                    "registration_endpoint": "https://auth.example/register",
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                    "authorization_response_iss_parameter_supported": True,
                },
            )
        if path == "/register":
            self.registration = json.loads(request.content)
            return httpx2.Response(201, json={**self.registration, "client_id": "gobby-client"})
        if path == "/custom/token":
            params = parse_qs(request.content.decode())
            assert params["resource"] == ["https://resource.example"]
            if params["grant_type"] == ["refresh_token"]:
                self.refreshes += 1
                assert params["refresh_token"] == ["refresh"]
                if self.deny_refresh:
                    return httpx2.Response(400, json={"error": "invalid_grant"})
                return httpx2.Response(
                    200,
                    json={
                        "access_token": "refreshed",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                )
            verifier = params["code_verifier"][0]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            assert challenge.rstrip(b"=").decode() == self.authorization["code_challenge"][0]
            assert params["redirect_uri"] == self.registration["redirect_uris"]
            assert params["code"] == ["consent-code"]
            return httpx2.Response(
                200,
                json={
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        raise AssertionError(f"Unexpected OAuth request: {request.method} {request.url}")


@pytest.mark.asyncio
@pytest.mark.parametrize("protected", [True, False])
async def test_cli_login_discovers_tools_after_public_initialization(
    secret_store: SecretStore, protected: bool
) -> None:
    server = AuthorizationServer()
    methods: list[str] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path != "/mcp":
            return server.respond(request)
        if request.method != "POST":
            return httpx2.Response(405)
        body = json.loads(request.content)
        method = body["method"]
        if method == "server/discover":
            return httpx2.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": -32601, "message": "Method not found"},
                },
            )
        methods.append(method)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "public-init", "version": "1"},
            }
        elif method == "notifications/initialized":
            return httpx2.Response(202)
        elif method == "tools/list":
            if protected and not request.headers.get("Authorization"):
                return server.respond(request)
            result = {"tools": []}
        else:
            raise AssertionError(f"Unexpected MCP method: {method}")
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    config = MCPServerConfig(
        name="public-init", project_id="project", url="https://resource.example/mcp"
    )
    callback = AsyncMock()
    callback.redirect_uri = "http://127.0.0.1:54321/callback"
    callback.redirect = server.redirect
    callback.wait = server.callback
    callback.__aenter__.return_value = callback

    def http_client(headers: object, auth: httpx2.Auth) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(auth=auth, transport=httpx2.MockTransport(respond))

    with (
        patch("gobby.cli.mcp_oauth.OAuthCallback", return_value=callback),
        patch("gobby.cli.mcp_oauth.build_mcp_http_client", side_effect=http_client),
    ):
        if protected:
            await authorize_server(config, secret_store, 5)
        else:
            with pytest.raises(click.ClickException, match="did not request OAuth"):
                await authorize_server(config, secret_store, 5)
    assert methods[:3] == ["initialize", "notifications/initialized", "tools/list"]
    storage = MCPOAuthStorage(secret_store, config)
    await storage.load()
    if protected:
        assert storage.state.tokens is not None
        assert storage.state.tokens.access_token == "access"
        assert methods.count("tools/list") == 2
    else:
        assert storage.state.tokens is None
        assert server.registration == {}


async def login(store: SecretStore, server: AuthorizationServer) -> MCPServerConfig:
    config = MCPServerConfig(
        name="fieldy", project_id="project", url="https://resource.example/mcp"
    )
    provider = PersistentOAuthProvider(
        config,
        MCPOAuthStorage(store, config),
        "http://127.0.0.1:54321/callback",
        server.redirect,
        server.callback,
    )
    async with httpx2.AsyncClient(
        auth=provider, transport=httpx2.MockTransport(server.respond)
    ) as client:
        response = await client.get(config.url or "")
    assert response.json() == {"authenticated": True}
    return config


@pytest.mark.asyncio
async def test_login_reconnect_and_refresh_after_restart(secret_store: SecretStore) -> None:
    server = AuthorizationServer()
    config = await login(secret_store, server)
    assert server.authorization["code_challenge_method"] == ["S256"]
    assert "offline_access" in server.authorization["scope"][0]
    storage = MCPOAuthStorage(secret_store, config)
    await storage.load()
    assert storage.state.expires_at is not None
    assert storage.state.tokens is not None
    assert storage.state.tokens.refresh_token == "refresh"

    # A fresh provider uses persisted credentials without prompting or discovery.
    server.requests.clear()
    provider = PersistentOAuthProvider(config, storage)
    async with httpx2.AsyncClient(
        auth=provider, transport=httpx2.MockTransport(server.respond)
    ) as client:
        assert (await client.get(config.url or "")).status_code == 200
    assert len(server.requests) == 1
    assert server.requests[0].headers["Authorization"] == "Bearer access"

    storage.state.expires_at = time.time() - 60
    await storage.save()
    provider = PersistentOAuthProvider(config, MCPOAuthStorage(secret_store, config))
    async with httpx2.AsyncClient(
        auth=provider, transport=httpx2.MockTransport(server.respond)
    ) as client:
        assert (await client.get(config.url or "")).status_code == 200
    assert server.refreshes == 1
    await storage.load()
    assert storage.state.tokens is not None
    assert storage.state.tokens.access_token == "refreshed"
    assert storage.state.tokens.refresh_token == "refresh"
    assert storage.state.expires_at is not None and storage.state.expires_at > time.time()


@pytest.mark.asyncio
async def test_missing_credentials_fail_without_registration(secret_store: SecretStore) -> None:
    config = MCPServerConfig(
        name="fieldy", project_id="project", url="https://resource.example/mcp"
    )
    server = AuthorizationServer()
    auth = PersistentOAuthProvider(config, MCPOAuthStorage(secret_store, config))
    async with httpx2.AsyncClient(
        auth=auth, transport=httpx2.MockTransport(server.respond)
    ) as client:
        with pytest.raises(MCPAuthorizationRequired, match="gobby mcp-proxy auth fieldy"):
            await client.get(config.url or "")
    assert server.requests == []


@pytest.mark.asyncio
async def test_failed_refresh_requires_consent_and_clears_token(secret_store: SecretStore) -> None:
    server = AuthorizationServer()
    config = await login(secret_store, server)
    storage = MCPOAuthStorage(secret_store, config)
    await storage.load()
    storage.state.expires_at = time.time() - 60
    await storage.save()
    server.deny_refresh = True
    auth = PersistentOAuthProvider(config, MCPOAuthStorage(secret_store, config))
    async with httpx2.AsyncClient(
        auth=auth, transport=httpx2.MockTransport(server.respond)
    ) as client:
        with pytest.raises(MCPAuthorizationRequired, match="needs authorization"):
            await client.get(config.url or "")
    await storage.load()
    assert storage.state.tokens is None


@pytest.mark.asyncio
async def test_storage_isolates_endpoint_instance_and_project(secret_store: SecretStore) -> None:
    config = await login(secret_store, AuthorizationServer())
    for changed in (
        replace(config, url="https://other.example/mcp"),
        replace(config, id="another-instance"),
        replace(config, project_id="another-project"),
    ):
        storage = MCPOAuthStorage(secret_store, changed)
        await storage.load()
        assert storage.state.tokens is None


@pytest.mark.asyncio
async def test_bad_state_never_exchanges_code(secret_store: SecretStore) -> None:
    server = AuthorizationServer()
    config = MCPServerConfig(
        name="fieldy", project_id="project", url="https://resource.example/mcp"
    )

    async def bad_callback() -> AuthorizationCodeResult:
        return AuthorizationCodeResult(code="consent-code", state="wrong")

    auth = PersistentOAuthProvider(
        config,
        MCPOAuthStorage(secret_store, config),
        "http://127.0.0.1:54321/callback",
        server.redirect,
        bad_callback,
    )
    async with httpx2.AsyncClient(
        auth=auth, transport=httpx2.MockTransport(server.respond)
    ) as client:
        with pytest.raises(OAuthFlowError, match="State parameter mismatch"):
            await client.get(config.url or "")
    assert all(request.url.path != "/custom/token" for request in server.requests)


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_transport_factory_supplies_oauth_provider(
    secret_store: SecretStore, transport: str
) -> None:
    config = MCPServerConfig(
        name="fieldy",
        project_id="project",
        url="https://resource.example/mcp",
        transport=transport,
        requires_oauth=True,
    )
    connection = create_transport_connection(config, secret_store=secret_store)
    assert isinstance(connection, HTTPTransportConnection)
    provider = connection._oauth_auth()
    assert isinstance(provider, PersistentOAuthProvider)
    assert provider.context.server_url == config.url
    assert provider.persistent_storage.store is secret_store


@pytest.mark.parametrize("transport", ["stdio", "websocket"])
def test_oauth_rejects_unsupported_transports(transport: str) -> None:
    config = MCPServerConfig(
        name="invalid", project_id="project", transport=transport, requires_oauth=True
    )
    with pytest.raises(ValueError, match="HTTP or SSE"):
        config.validate()
