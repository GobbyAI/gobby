"""MCP SDK OAuth authorization backed by Gobby's encrypted secret store."""

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
import shlex
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, Literal
from urllib.parse import quote, quote_plus, unquote_plus, urlsplit

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
from gobby.mcp_proxy.oauth_keepalive import (
    backoff_active,
    conninfo_for_store,
    consent_required,
    hold_oauth_lock,
    oauth_lock_is_held,
    oauth_server_lock,
    schedule_backoff,
)
from gobby.mcp_proxy.transports.base import gobby_client_info
from gobby.mcp_proxy.transports.http import build_mcp_http_client
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.storage.secrets import SecretStore

DEFAULT_OAUTH_TIMEOUT_SECONDS = 300.0
logger = logging.getLogger(__name__)
_SECRET_FIELD_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "code",
        "code_verifier",
        "client_secret",
        "client_assertion",
        "authorization",
        "password",
    }
)
_PUBLIC_ERROR_FIELDS = ("error", "error_description", "message")
_NAMED_FAILURE_HEADERS = ("retry-after", "content-type", "server", "cf-ray")


def _request_credentials(response: httpx2.Response) -> set[str]:
    """Credentials the failed token request sent, which the endpoint may echo in its errors."""
    try:
        request = response.request
        body = request.content
    except (RuntimeError, httpx2.RequestNotRead):
        return set()
    secrets: set[str] = set()
    # Keep each value both as sent and decoded; an endpoint may echo either.
    for pair in body.decode("utf-8", "replace").split("&"):
        key, _, sent = pair.partition("=")
        if unquote_plus(key) in _SECRET_FIELD_KEYS and sent:
            secrets.update((sent, unquote_plus(sent)))
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    if credential:
        secrets.add(credential)
        if scheme.casefold() == "basic":
            try:
                decoded = base64.b64decode(credential, validate=True).decode()
            except (binascii.Error, UnicodeDecodeError):
                decoded = ""
            sent = decoded.partition(":")[2]
            if sent:
                secrets.update((sent, unquote_plus(sent)))
    return secrets


def _safe_oauth_failure_summary(response: httpx2.Response) -> str:
    """Status, rate-limit headers, and Fieldy's error fields, without credentials."""
    secrets = _request_credentials(response)
    public: dict[str, str] = {}
    try:
        payload = json.loads(response.content or b"")
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None
    if isinstance(payload, dict):
        for key, value in payload.items():
            if not isinstance(key, str) or not isinstance(value, str) or not value:
                continue
            if key in _SECRET_FIELD_KEYS:
                secrets.add(value)
            elif key in _PUBLIC_ERROR_FIELDS:
                public[key] = value

    # Each secret also in its re-encoded forms, matched case-insensitively because
    # percent-escapes are case-insensitive. Longest first, so a secret that contains
    # another is never partly revealed.
    forms = {
        form
        for secret in secrets
        for form in (secret, quote(secret, safe=""), quote_plus(secret, safe=""))
    }
    patterns = [
        re.compile(re.escape(form), re.IGNORECASE) for form in sorted(forms, key=len, reverse=True)
    ]

    def clean(text: str) -> str:
        for pattern in patterns:
            text = pattern.sub("[redacted]", text)
        return text

    parts = [f"status={response.status_code}"]
    for name in _NAMED_FAILURE_HEADERS:
        value = response.headers.get(name)
        if value:
            parts.append(f"{name.replace('-', '_')}={clean(value)}")
    for key, value in response.headers.items():
        lower = key.lower()
        if lower.startswith("ratelimit-"):
            parts.append(f"{lower.replace('-', '_')}={clean(value)}")
    for key in _PUBLIC_ERROR_FIELDS:
        if key in public:
            parts.append(f"{key}={clean(public[key])}")
    return " ".join(parts)


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
    retry_not_before: float | None = None
    retry_attempt: int = 0


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
                token_endpoint_auth_method="none",  # nosec B106 # RFC 7591 public value.
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
        if not self._initialized:
            await self._initialize()
        if backoff_active(self.persistent_storage.state, time.time()) and (
            not self.context.is_token_valid()
        ):
            raise OAuthFlowError("OAuth token endpoint is in backoff")
        lock = await self._enter_refresh_lock()
        flow = super().async_auth_flow(request)
        saw_token = False
        retried_401 = False
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
                if registering and self.persistent_storage.state.client is not None:
                    raise OAuthFlowError("refusing to re-register an existing OAuth client")
                saw_token = (
                    outgoing.method == "POST" and str(outgoing.url) == self._get_token_endpoint()
                )
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
                if (
                    response.status_code == 401
                    and str(outgoing.url) == str(request.url)
                    and not retried_401
                    and await self._reload_rotated_token()
                ):
                    await self._exit_refresh_lock(lock)
                    lock = None
                    await flow.aclose()
                    retried_401 = True
                    flow = super().async_auth_flow(request)
                    outgoing = await anext(flow)
                    continue
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
                    token_exchange = (
                        outgoing.method == "POST"
                        and str(outgoing.url) == self._get_token_endpoint()
                    )
                    summary = None
                    if token_exchange:
                        summary = await self._record_token_endpoint_failure(response)
                    elif registering and (
                        response.status_code == 429 or response.status_code >= 500
                    ):
                        schedule_backoff(
                            self.persistent_storage.state,
                            status=response.status_code,
                            retry_after=response.headers.get("retry-after"),
                            now=time.time(),
                        )
                        await self.persistent_storage.save()
                    body: dict[str, str] = {"error": "oauth_endpoint_error"}
                    if summary is not None:
                        body["error_description"] = summary
                    response = httpx2.Response(
                        response.status_code,
                        headers={"Content-Type": "application/json"},
                        json=body,
                        request=outgoing,
                    )
                try:
                    outgoing = await flow.asend(response)
                except StopAsyncIteration:
                    break
                if saw_token:
                    await self._exit_refresh_lock(lock)
                    lock = None
                    saw_token = False
        finally:
            await self._exit_refresh_lock(lock)
            await flow.aclose()

    def _apply_stored_state(self) -> None:
        state = self.persistent_storage.state
        self.context.current_tokens = state.tokens
        self.context.client_info = state.client
        self.context.token_expiry_time = state.expires_at
        self.context.oauth_metadata = state.metadata
        self.context.protected_resource_metadata = state.resource
        if state.issuer:
            self.context.auth_server_url = state.issuer

    async def _enter_refresh_lock(self) -> Any:
        """Return this flow's advisory lock, or None when the stored token is usable."""
        if oauth_lock_is_held():
            return None
        conninfo = conninfo_for_store(self.persistent_storage.store)
        if not isinstance(conninfo, str):
            return None
        state = self.persistent_storage.state
        if state.tokens is None:
            return None
        if state.expires_at is not None and state.expires_at > time.time():
            return None
        lock = oauth_server_lock(self.persistent_storage.name, conninfo)
        await lock.__aenter__()
        try:
            await self.persistent_storage.load()
            self._apply_stored_state()
            now = time.time()
            if backoff_active(self.persistent_storage.state, now):
                raise OAuthFlowError("OAuth token endpoint is in backoff")
            if self.context.is_token_valid():
                await lock.__aexit__(None, None, None)
                return None
        except Exception:
            await lock.__aexit__(None, None, None)
            raise
        return lock

    async def _exit_refresh_lock(self, lock: Any) -> None:
        if lock is not None:
            await lock.__aexit__(None, None, None)

    async def _reload_rotated_token(self) -> bool:
        await self.persistent_storage.load()
        stored = self.persistent_storage.state
        current = self.context.current_tokens
        stored_access = stored.tokens.access_token if stored.tokens is not None else None
        current_access = current.access_token if current is not None else None
        expires_at = stored.expires_at
        if stored_access is None or stored_access == current_access:
            return False
        if expires_at is None or float(expires_at) <= time.time():
            return False
        self._apply_stored_state()
        return True

    async def _record_token_endpoint_failure(self, response: httpx2.Response) -> str:
        await response.aread()
        summary = _safe_oauth_failure_summary(response)
        logger.warning("OAuth token endpoint failed: %s", summary)
        payload = _token_error_payload(response)
        if consent_required(payload):
            self.persistent_storage.state.tokens = None
            self.persistent_storage.state.expires_at = None
            self.context.clear_tokens()
            await self.persistent_storage.save()
            raise MCPAuthorizationRequired(self.auth_command)
        schedule_backoff(
            self.persistent_storage.state,
            status=response.status_code,
            retry_after=response.headers.get("retry-after"),
            now=time.time(),
        )
        await self.persistent_storage.save()
        return summary

    async def refresh_persisted_token(
        self, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> bool:
        """Refresh at the token endpoint and persist. Does not call the MCP URL."""
        if not self._initialized:
            await self._initialize()
        request = await self._refresh_token()
        async with httpx2.AsyncClient(transport=transport, timeout=30) as client:
            response = await client.send(request)
        if response.status_code >= 400:
            await self._record_token_endpoint_failure(response)
            return False
        return await self._handle_refresh_response(response)

    async def _save_context(self) -> None:
        state = self.persistent_storage.state
        state.expires_at = self.context.token_expiry_time
        state.metadata = self.context.oauth_metadata
        state.resource = self.context.protected_resource_metadata
        state.issuer = self.context.auth_server_url
        state.tokens = self.context.current_tokens
        if state.expires_at is not None and state.expires_at > time.time():
            state.retry_not_before = None
            state.retry_attempt = 0
        await self.persistent_storage.save()

    async def _handle_token_response(self, response: httpx2.Response) -> None:
        await super()._handle_token_response(response)
        await self._save_context()

    async def _handle_refresh_response(self, response: httpx2.Response) -> bool:
        refreshed = await super()._handle_refresh_response(response)
        await self._save_context()
        return refreshed


def _token_error_payload(response: httpx2.Response) -> dict[str, Any]:
    try:
        payload = json.loads(response.content or b"{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


async def automatic_oauth_authorization(config: MCPServerConfig, store: SecretStore) -> None:
    """Daemon authorization never opens a browser or registers a new client."""
    del store
    raise MCPAuthorizationRequired(oauth_auth_command(config))


async def authorize_server(
    config: MCPServerConfig,
    store: SecretStore,
    timeout: float,
    open_browser: Callable[[str], Awaitable[None]],
) -> None:
    """Complete interactive OAuth and persist credentials for one MCP server."""
    storage = MCPOAuthStorage(store, config)
    await storage.load()
    conninfo = conninfo_for_store(store)
    if isinstance(conninfo, str):
        async with oauth_server_lock(storage.name, conninfo, timeout_seconds=max(1.0, timeout)):
            with hold_oauth_lock():
                await _authorize_loaded_server(config, storage, timeout, open_browser)
        return
    await _authorize_loaded_server(config, storage, timeout, open_browser)


async def _authorize_loaded_server(
    config: MCPServerConfig,
    storage: MCPOAuthStorage,
    timeout: float,
    open_browser: Callable[[str], Awaitable[None]],
) -> None:
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
