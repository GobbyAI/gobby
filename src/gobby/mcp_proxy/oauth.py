"""MCP SDK OAuth authorization backed by Gobby's encrypted secret store."""

import asyncio
import hashlib
import shlex
import webbrowser
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Literal
from urllib.parse import urlsplit

import httpx2
from mcp.client import Client
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, OAuthFlowError
from mcp.client.auth.utils import create_client_registration_request
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from pydantic import AnyUrl, BaseModel

from gobby.mcp_proxy.models import MCPAuthorizationRequired, MCPError, MCPServerConfig
from gobby.mcp_proxy.oauth_callback import OAuthCallback
from gobby.mcp_proxy.transports.base import gobby_client_info
from gobby.mcp_proxy.transports.http import build_mcp_http_client
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.storage.secrets import SecretStore

DEFAULT_OAUTH_TIMEOUT_SECONDS = 300.0


def oauth_auth_command(config: MCPServerConfig) -> str:
    command = ["gobby", "mcp-proxy", "auth", config.name]
    if config.project_id == GLOBAL_PROJECT_ID:
        command.append("--global")
    return shlex.join(command)


class OAuthState(BaseModel):
    tokens: OAuthToken | None = None
    client: OAuthClientInformationFull | None = None
    expires_at: float | None = None
    metadata: OAuthMetadata | None = None
    resource: ProtectedResourceMetadata | None = None
    issuer: str | None = None


class MCPOAuthStorage:
    """Keep credentials isolated by server instance, scope, and exact endpoint."""

    def __init__(self, store: SecretStore, config: MCPServerConfig) -> None:
        self.store = store
        self.project_id = config.project_id
        identity = f"{config.project_id}\n{config.id}\n{config.url}"
        self.name = "mcp_oauth_" + hashlib.sha256(identity.encode()).hexdigest()
        self.state = OAuthState()

    async def load(self) -> None:
        value = await asyncio.to_thread(self.store.get, self.name, project_id=self.project_id)
        self.state = OAuthState.model_validate_json(value) if value else OAuthState()

    async def save(self) -> None:
        await asyncio.to_thread(
            self.store.set, self.name, self.state.model_dump_json(), project_id=self.project_id
        )

    async def get_tokens(self) -> OAuthToken | None:
        return self.state.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.state.tokens = tokens
        # The provider saves tokens together with expiry and discovery metadata.

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.state.client

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.state.client = client_info
        await self.save()


class PersistentOAuthProvider(OAuthClientProvider):
    """Restore expiry and discovery metadata as well as SDK tokens on reconnect."""

    def __init__(
        self,
        config: MCPServerConfig,
        storage: MCPOAuthStorage,
        redirect_uri: str = "http://127.0.0.1/callback",
        redirect_handler: Callable[[str], Awaitable[None]] | None = None,
        callback_handler: Callable[[], Awaitable[AuthorizationCodeResult]] | None = None,
    ) -> None:
        if not config.url or config.transport not in ("http", "sse"):
            raise ValueError("OAuth requires an HTTP or SSE MCP server")
        self.persistent_storage = storage
        self.interactive = redirect_handler is not None
        self.auth_command = oauth_auth_command(config)

        async def needs_login(_url: str) -> None:
            raise MCPAuthorizationRequired(self.auth_command)

        async def no_callback() -> AuthorizationCodeResult:
            raise OAuthFlowError("Interactive OAuth is only available through mcp-proxy auth")

        super().__init__(
            server_url=config.url,
            client_metadata=OAuthClientMetadata(
                client_name="Gobby",
                redirect_uris=[AnyUrl(redirect_uri)],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
            ),
            storage=storage,
            redirect_handler=redirect_handler or needs_login,
            callback_handler=callback_handler or no_callback,
        )

    async def _initialize(self) -> None:
        await self.persistent_storage.load()
        state = self.persistent_storage.state
        if not self.interactive and state.tokens is None:
            raise MCPAuthorizationRequired(self.auth_command)
        if self.interactive and state.tokens is None:
            # An unfinished registration may have selected an unusable auth method.
            state.client = None
            await self.persistent_storage.save()
        await super()._initialize()
        self.context.token_expiry_time = state.expires_at
        self.context.oauth_metadata = state.metadata
        self.context.protected_resource_metadata = state.resource
        self.context.auth_server_url = state.issuer

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        flow = super().async_auth_flow(request)
        try:
            outgoing = await anext(flow)
            while True:
                metadata = self.context.oauth_metadata
                registration_url = (
                    str(metadata.registration_endpoint)
                    if metadata and metadata.registration_endpoint
                    else self.context.get_authorization_base_url(self.context.server_url)
                    + "/register"
                )
                registering = outgoing.method == "POST" and str(outgoing.url) == registration_url
                if registering:
                    supported = (
                        metadata.token_endpoint_auth_methods_supported
                        if metadata and metadata.token_endpoint_auth_methods_supported is not None
                        else ["client_secret_basic"]
                    )
                    choices: tuple[
                        Literal["none", "client_secret_basic", "client_secret_post"], ...
                    ] = ("none", "client_secret_basic", "client_secret_post")
                    method = next(
                        (m for m in choices if m in supported),
                        None,
                    )
                    if method is None:
                        raise OAuthFlowError(
                            "Server has no supported OAuth client authentication method"
                        )
                    self.context.client_metadata.token_endpoint_auth_method = method
                    outgoing = create_client_registration_request(
                        metadata,
                        self.context.client_metadata,
                        self.context.get_authorization_base_url(self.context.server_url),
                    )
                response = yield outgoing
                if response.is_success:
                    model = (
                        OAuthClientInformationFull
                        if registering
                        else OAuthToken
                        if outgoing.method == "POST"
                        and str(outgoing.url) == self._get_token_endpoint()
                        else None
                    )
                    if model is not None:
                        try:
                            model.model_validate_json(await response.aread())
                        except ValueError:
                            # Validation errors can embed access tokens or client secrets.
                            response = httpx2.Response(400, request=outgoing)
                # SDK exceptions/logs include OAuth response bodies. Strip error bodies
                # before they reach that layer; successful credentials remain encrypted.
                if outgoing.url != request.url and response.status_code >= 400:
                    response = httpx2.Response(
                        response.status_code,
                        headers={"Content-Type": "application/json"},
                        json={"error": "oauth_endpoint_error"},
                        request=outgoing,
                    )
                try:
                    outgoing = await flow.asend(response)
                except StopAsyncIteration:
                    break
        finally:
            await flow.aclose()

    async def _save_context(self) -> None:
        state = self.persistent_storage.state
        state.expires_at = self.context.token_expiry_time
        state.metadata = self.context.oauth_metadata
        state.resource = self.context.protected_resource_metadata
        state.issuer = self.context.auth_server_url
        state.tokens = self.context.current_tokens
        await self.persistent_storage.save()

    async def _handle_token_response(self, response: httpx2.Response) -> None:
        await super()._handle_token_response(response)
        await self._save_context()

    async def _handle_refresh_response(self, response: httpx2.Response) -> bool:
        refreshed = await super()._handle_refresh_response(response)
        await self._save_context()
        return refreshed


async def authorize_server(
    config: MCPServerConfig,
    store: SecretStore,
    timeout: float,
    open_browser: Callable[[str], Awaitable[None]],
) -> None:
    """Complete interactive OAuth and persist credentials for one MCP server."""
    storage = MCPOAuthStorage(store, config)
    await storage.load()
    port = 0
    if storage.state.client and storage.state.client.redirect_uris:
        redirect = urlsplit(str(storage.state.client.redirect_uris[0]))
        if (
            redirect.scheme != "http"
            or redirect.hostname != "127.0.0.1"
            or redirect.path != "/callback"
            or not redirect.port
            or redirect.query
            or redirect.fragment
            or redirect.username
        ):
            raise MCPError("Stored OAuth client has an invalid loopback callback URI")
        port = redirect.port

    async with asyncio.timeout(timeout), OAuthCallback(open_browser, port) as callback:
        auth = PersistentOAuthProvider(
            config, storage, callback.redirect_uri, callback.redirect, callback.wait
        )
        if config.url is None:
            raise ValueError("OAuth requires a server URL")
        async with build_mcp_http_client(config.headers, auth) as http_client:
            transport = (
                sse_client(config.url, headers=config.headers, auth=auth)
                if config.transport == "sse"
                else streamable_http_client(config.url, http_client=http_client)
            )
            async with Client(transport, client_info=gobby_client_info()) as client:
                # Some servers permit initialization anonymously and challenge discovery.
                await client.list_tools()
        if storage.state.tokens is None:
            raise MCPError(
                "The MCP server did not request OAuth during initialization or tool discovery; "
                "check the server URL and authentication requirements"
            )


async def authorize_server_in_browser(
    config: MCPServerConfig,
    store: SecretStore,
    timeout: float = DEFAULT_OAUTH_TIMEOUT_SECONDS,
    *,
    browser_open: Callable[[str], bool] | None = None,
) -> None:
    """Launch the system browser and complete OAuth for a daemon-owned request."""
    opener = browser_open or webbrowser.open

    async def open_browser(url: str) -> None:
        opened = await asyncio.to_thread(opener, url)
        if not opened:
            raise MCPAuthorizationRequired(oauth_auth_command(config))

    await authorize_server(config, store, timeout, open_browser)
