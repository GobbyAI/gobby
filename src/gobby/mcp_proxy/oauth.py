"""MCP SDK OAuth authorization backed by Gobby's encrypted secret store."""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable

import httpx2
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, OAuthFlowError
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from pydantic import AnyUrl, BaseModel

from gobby.mcp_proxy.models import MCPServerConfig
from gobby.storage.secrets import SecretStore


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
        self.server_name = config.name

        async def needs_login(_url: str) -> None:
            raise OAuthFlowError(
                f"MCP server {config.name!r} needs authorization; "
                f"run gobby mcp-proxy auth {config.name}"
            )

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
            raise OAuthFlowError(
                f"MCP server {self.server_name!r} needs authorization; "
                f"run gobby mcp-proxy auth {self.server_name}"
            )
        await super()._initialize()
        self.context.token_expiry_time = state.expires_at
        self.context.oauth_metadata = state.metadata
        self.context.protected_resource_metadata = state.resource
        self.context.auth_server_url = state.issuer

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
