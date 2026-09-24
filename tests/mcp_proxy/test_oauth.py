"""OAuth wire-flow and persistence tests against an isolated authorization server."""

import base64
import hashlib
import json
import re
import time
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, quote, quote_plus, unquote_plus, urlsplit

import httpx2
import pytest
from mcp.client.auth import AuthorizationCodeResult, OAuthFlowError

from gobby.mcp_proxy.models import MCPAuthorizationRequired, MCPError, MCPServerConfig
from gobby.mcp_proxy.oauth import (
    MCPOAuthStorage,
    PersistentOAuthProvider,
    authorize_server,
)
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
    def __init__(
        self,
        auth_method: str = "none",
        *,
        resource: str = "https://resource.example",
        issuer: str = "https://auth.example",
        authorization_endpoint: str = "https://auth.example/authorize",
        token_endpoint: str = "https://auth.example/custom/token",
        registration_endpoint: str = "https://auth.example/register",
        prm_url: str = "https://resource.example/prm",
        metadata_paths: tuple[str, ...] = ("/.well-known/oauth-authorization-server",),
        scopes: tuple[str, ...] = ("conversations:read", "offline_access"),
        grant_types: tuple[str, ...] = (),
    ) -> None:
        self.auth_method = auth_method
        self.resource = resource
        self.issuer = issuer
        self.authorization_endpoint = authorization_endpoint
        self.token_endpoint = token_endpoint
        self.registration_endpoint = registration_endpoint
        self.prm_url = prm_url
        self.metadata_paths = metadata_paths
        self.scopes = scopes
        self.grant_types = grant_types
        self.authorization: dict[str, list[str]] = {}
        self.registration: dict[str, Any] = {}
        self.refreshes = 0
        self.deny_refresh = False
        self.requests: list[httpx2.Request] = []

    async def redirect(self, url: str) -> None:
        self.authorization = parse_qs(urlsplit(url).query)

    async def callback(self) -> AuthorizationCodeResult:
        return AuthorizationCodeResult(
            code="consent-code", state=self.authorization["state"][0], iss=self.issuer
        )

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/mcp":
            if request.headers.get("Authorization") in ("Bearer access", "Bearer refreshed"):
                return httpx2.Response(200, json={"authenticated": True})
            return httpx2.Response(
                401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{self.prm_url}"'},
            )
        if path == urlsplit(self.prm_url).path:
            return httpx2.Response(
                200,
                json={
                    "resource": self.resource,
                    "authorization_servers": [self.issuer],
                    "scopes_supported": list(self.scopes),
                },
            )
        if path in self.metadata_paths:
            metadata: dict[str, Any] = {
                "issuer": self.issuer,
                "authorization_endpoint": self.authorization_endpoint,
                "token_endpoint": self.token_endpoint,
                "registration_endpoint": self.registration_endpoint,
                "response_types_supported": ["code"],
                "token_endpoint_auth_methods_supported": [self.auth_method],
                "code_challenge_methods_supported": ["S256"],
                "authorization_response_iss_parameter_supported": True,
            }
            if self.grant_types:
                metadata["grant_types_supported"] = list(self.grant_types)
            return httpx2.Response(200, json=metadata)
        if path == urlsplit(self.registration_endpoint).path:
            self.registration = json.loads(request.content)
            assert self.registration["token_endpoint_auth_method"] == self.auth_method
            return httpx2.Response(
                201,
                json={
                    **self.registration,
                    "client_id": "gobby-client",
                    "client_secret": "client-secret" if self.auth_method != "none" else None,
                },
            )
        if path == urlsplit(self.token_endpoint).path:
            params = parse_qs(request.content.decode())
            if self.auth_method == "client_secret_basic":
                expected = base64.b64encode(b"gobby-client:client-secret").decode()
                assert request.headers["Authorization"] == f"Basic {expected}"
                assert "client_secret" not in params
            elif self.auth_method == "client_secret_post":
                assert params["client_secret"] == ["client-secret"]
                assert "Authorization" not in request.headers
            else:
                assert "client_secret" not in params
                assert "Authorization" not in request.headers
            assert params["resource"] == [self.resource]
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
        patch("gobby.mcp_proxy.oauth.OAuthCallback", return_value=callback),
        patch("gobby.mcp_proxy.oauth.build_mcp_http_client", side_effect=http_client),
    ):
        if protected:
            await authorize_server(config, secret_store, 5, AsyncMock())
        else:
            with pytest.raises(MCPError, match="did not request OAuth"):
                await authorize_server(config, secret_store, 5, AsyncMock())
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


async def login(
    store: SecretStore, server: AuthorizationServer, config: MCPServerConfig | None = None
) -> MCPServerConfig:
    config = config or MCPServerConfig(
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


_FIELDY_SCOPES = (
    "openid",
    "profile",
    "email",
    "offline_access",
    "conversations:read",
    "transcripts:read",
    "sharables:write",
)


@pytest.mark.asyncio
async def test_fieldy_published_oauth_shape_completes_for_a_public_client(
    secret_store: SecretStore,
) -> None:
    """Fieldy's published metadata completes on the public-client path with no network."""
    server = AuthorizationServer(
        resource="https://api.fieldy.ai",
        issuer="https://api.fieldy.ai/api/auth",
        authorization_endpoint="https://api.fieldy.ai/api/auth/oauth2/authorize",
        token_endpoint="https://api.fieldy.ai/api/auth/oauth2/token",
        registration_endpoint="https://api.fieldy.ai/api/auth/oauth2/register",
        prm_url="https://api.fieldy.ai/.well-known/oauth-protected-resource",
        metadata_paths=("/.well-known/oauth-authorization-server/api/auth",),
        scopes=_FIELDY_SCOPES,
        grant_types=("authorization_code", "client_credentials", "refresh_token"),
    )
    config = MCPServerConfig(name="fieldy", project_id="project", url="https://api.fieldy.ai/mcp")
    await login(secret_store, server, config)

    assert server.registration["token_endpoint_auth_method"] == "none"
    assert server.authorization["code_challenge_method"] == ["S256"]
    requested = set(server.authorization["scope"][0].split())
    assert requested
    assert requested <= set(_FIELDY_SCOPES)
    token_requests = [
        request
        for request in server.requests
        if request.method == "POST" and request.url.path == "/api/auth/oauth2/token"
    ]
    register_requests = [
        request
        for request in server.requests
        if request.method == "POST" and request.url.path == "/api/auth/oauth2/register"
    ]
    assert len(token_requests) == 1
    assert len(register_requests) == 1
    assert server.requests.index(register_requests[0]) < server.requests.index(token_requests[0])
    params = parse_qs(token_requests[0].content.decode())
    assert params["resource"] == ["https://api.fieldy.ai"]
    assert "client_secret" not in params
    assert "Authorization" not in token_requests[0].headers
    storage = MCPOAuthStorage(secret_store, config)
    await storage.load()
    assert storage.state.tokens is not None
    assert storage.state.tokens.access_token == "access"


@pytest.mark.asyncio
async def test_unfinished_public_registration_is_replaced(secret_store: SecretStore) -> None:
    config = await login(secret_store, AuthorizationServer())
    storage = MCPOAuthStorage(secret_store, config)
    await storage.load()
    storage.state.tokens = None
    await storage.save()
    server = AuthorizationServer("client_secret_basic")
    await login(secret_store, server, config)
    await storage.load()
    assert server.registration["token_endpoint_auth_method"] == "client_secret_basic"
    assert storage.state.client is not None
    assert storage.state.client.client_secret == "client-secret"
    assert storage.state.tokens is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/register", "/custom/token"])
@pytest.mark.parametrize("status", [200, 400])
async def test_oauth_response_secrets_are_redacted_before_sdk_logging(
    secret_store: SecretStore, caplog: pytest.LogCaptureFixture, endpoint: str, status: int
) -> None:
    class FailingServer(AuthorizationServer):
        def respond(self, request: httpx2.Request) -> httpx2.Response:
            if request.url.path == endpoint:
                return httpx2.Response(
                    status,
                    json={"error": "invalid_client", "client_secret": "private-response-secret"},
                )
            return super().respond(request)

    with pytest.raises(OAuthFlowError) as error:
        await login(secret_store, FailingServer())
    assert "private-response-secret" not in str(error.value)
    assert "private-response-secret" not in caplog.text
    assert "oauth_endpoint_error" in str(error.value)


@pytest.mark.asyncio
async def test_token_exchange_failure_surfaces_safe_headers_and_redacts_credentials(
    secret_store: SecretStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A token-endpoint failure reports status and Fieldy's error fields, not credentials."""

    class RateLimited(AuthorizationServer):
        def respond(self, request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/custom/token":
                return httpx2.Response(
                    429,
                    headers={
                        "Retry-After": "30",
                        "RateLimit-Remaining": "0",
                        "Content-Type": "application/json",
                        "Server": "cloudflare",
                        "CF-Ray": "abc123-ORD",
                    },
                    json={
                        "error": "invalid_client",
                        "error_description": "registration rejected",
                        "message": "slow down",
                        "access_token": "secret-token-value",
                        "code": "auth-code-value",
                        "client_secret": "super-secret-value",
                    },
                )
            return super().respond(request)

    caplog.set_level("WARNING")
    with pytest.raises(OAuthFlowError) as error:
        await login(secret_store, RateLimited())
    visible = str(error.value) + caplog.text
    assert "status=429" in visible
    assert "retry_after=30" in visible
    assert "ratelimit_remaining=0" in visible
    assert "content_type=application/json" in visible
    assert "server=cloudflare" in visible
    assert "cf_ray=abc123-ORD" in visible
    assert "error=invalid_client" in visible
    assert "error_description=registration rejected" in visible
    assert "message=slow down" in visible
    for secret in ("secret-token-value", "auth-code-value", "super-secret-value"):
        assert secret not in visible


_ECHO_CODE = "k7Code/1+2 &=%"
_ECHO_SECRET = "s9Secret/1+2 &=%"
_ECHO_REFRESH = "r5Refresh/1+2 &=%"


def _reflections(sent: str) -> list[str]:
    """A credential as sent, decoded, and re-encoded the ways an endpoint might echo it."""
    value = unquote_plus(sent)
    encoded = quote(value, safe="")
    lower_hex = re.sub(r"%[0-9A-F]{2}", lambda escape: escape.group().lower(), encoded)
    return [sent, value, encoded, quote_plus(value, safe=""), lower_hex]


class EchoingServer(AuthorizationServer):
    """Fails one grant with public error fields that reflect every credential it was sent."""

    def __init__(self, auth_method: str, failing_grant: str) -> None:
        super().__init__(auth_method)
        self.failing_grant = failing_grant
        self.echoed: list[str] = []

    async def callback(self) -> AuthorizationCodeResult:
        return AuthorizationCodeResult(
            code=_ECHO_CODE, state=self.authorization["state"][0], iss=self.issuer
        )

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == "/register":
            registered = json.loads(super().respond(request).content)
            if registered["client_secret"]:
                registered["client_secret"] = _ECHO_SECRET
            return httpx2.Response(201, json=registered)
        if path != "/custom/token":
            return super().respond(request)
        self.requests.append(request)
        pairs = (pair.partition("=") for pair in request.content.decode().split("&"))
        fields = {key: value for key, _, value in pairs}
        if fields["grant_type"] != self.failing_grant:
            return httpx2.Response(
                200,
                json={
                    "access_token": "access",
                    "refresh_token": _ECHO_REFRESH,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        sent = [
            fields[key]
            for key in ("code", "code_verifier", "refresh_token", "client_secret")
            if key in fields
        ]
        authorization = request.headers.get("Authorization", "")
        if authorization:
            sent.append(base64.b64decode(authorization.split()[1]).decode().split(":", 1)[1])
            self.echoed.append(authorization.split()[1])
        self.echoed += [form for value in sent for form in _reflections(value)]
        return httpx2.Response(
            400,
            json={
                "error": "invalid_request",
                "error_description": f"rejected {' '.join(self.echoed)}",
                "message": f"header was {authorization or 'absent'}",
            },
        )


def _assert_echoes_redacted(server: EchoingServer, visible: str, auth_method: str) -> None:
    assert "error=invalid_request" in visible
    assert "error_description=rejected [redacted]" in visible
    for form in server.echoed:
        assert form not in visible
    # No fragment of any credential survives, whatever its encoding.
    for stem in ("k7code", "s9secret", "r5refresh"):
        assert stem not in visible.lower()
    if auth_method == "client_secret_basic":
        assert "message=header was Basic [redacted]" in visible


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_method", ["none", "client_secret_basic", "client_secret_post"])
async def test_token_failure_redacts_request_credentials_echoed_in_error_text(
    secret_store: SecretStore, caplog: pytest.LogCaptureFixture, auth_method: str
) -> None:
    """Code-exchange credentials stay hidden when the public error fields echo them encoded."""
    server = EchoingServer(auth_method, "authorization_code")
    caplog.set_level("WARNING")
    with pytest.raises(OAuthFlowError) as error:
        await login(secret_store, server)
    assert server.echoed
    _assert_echoes_redacted(server, str(error.value) + caplog.text, auth_method)


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_method", ["none", "client_secret_basic", "client_secret_post"])
async def test_refresh_failure_redacts_request_credentials_echoed_in_error_text(
    secret_store: SecretStore, caplog: pytest.LogCaptureFixture, auth_method: str
) -> None:
    """Refresh-grant credentials stay hidden when the public error fields echo them encoded."""
    server = EchoingServer(auth_method, "refresh_token")
    config = await login(secret_store, server)
    caplog.set_level("WARNING")
    provider = PersistentOAuthProvider(config, MCPOAuthStorage(secret_store, config))
    refreshed = await provider.refresh_persisted_token(
        transport=httpx2.MockTransport(server.respond)
    )
    assert refreshed is False
    assert any(_ECHO_REFRESH in form for form in server.echoed)
    _assert_echoes_redacted(server, caplog.text, auth_method)


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_method", ["none", "client_secret_basic", "client_secret_post"])
async def test_login_reconnect_and_refresh_after_restart(
    secret_store: SecretStore, auth_method: str
) -> None:
    server = AuthorizationServer(auth_method)
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
