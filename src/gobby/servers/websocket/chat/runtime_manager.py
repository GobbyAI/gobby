"""Daemon-owned runtime manager for web-chat providers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.agents.codex_oss import (
    codex_local_transport_strategy,
    codex_oss_launch_args,
    codex_oss_provider_for_local_endpoint,
    codex_oss_supported_provider_clause,
)
from gobby.agents.sandbox import (
    SandboxConfig,
    web_chat_policy_mismatch_message,
    web_chat_sandbox_config,
    web_chat_sandbox_policy_hash,
)
from gobby.ai.codex_endpoint import (
    codex_endpoint_app_server_env,
    codex_endpoint_config_overrides,
)
from gobby.ai.endpoints import parse_endpoint_model_selector, parse_endpoint_selector
from gobby.config.ai import GenerationEndpointConfig
from gobby.config.app import DaemonConfig
from gobby.providers import AGY_UNAVAILABLE_REASON
from gobby.servers.chat_session import ChatSession
from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.websocket.chat.backends import (
    ClaudeWebChatBackend,
    CodexManagedChatSession,
    CodexWebChatBackend,
    DroidManagedChatSession,
    DroidWebChatBackend,
    GrokManagedChatSession,
    GrokWebChatBackend,
    ProviderBackendHealth,
    QwenManagedChatSession,
    QwenWebChatBackend,
)
from gobby.servers.websocket.chat.backends.acp import ACPWebChatBackend


class WebChatRuntimeManager:
    """Owns startup-managed provider backends for web chat."""

    def __init__(
        self,
        *,
        codex_client: CodexAppServerClient | None = None,
        codex_transcript_retry_attempts: int = 5,
        codex_transcript_retry_delay_seconds: float = 0.1,
        daemon_config: DaemonConfig | None = None,
        config_resolver: Callable[[], DaemonConfig | None] | None = None,
    ) -> None:
        self._config_resolver = config_resolver
        self._sandbox_config = web_chat_sandbox_config(daemon_config)
        self._sandbox_policy_hash = web_chat_sandbox_policy_hash(daemon_config)
        self._generation_endpoints: dict[str, GenerationEndpointConfig] = {}
        if daemon_config is not None:
            ai_config = getattr(daemon_config, "ai", None)
            generation = getattr(ai_config, "generation", None)
            endpoints = getattr(generation, "endpoints", {})
            if isinstance(endpoints, dict):
                self._generation_endpoints = endpoints
        self._claude_backend = ClaudeWebChatBackend(
            sandbox_config=self._sandbox_config.model_copy(deep=True)
        )
        self._codex_backend = CodexWebChatBackend(
            client=codex_client,
            transcript_retry_attempts=codex_transcript_retry_attempts,
            transcript_retry_delay_seconds=codex_transcript_retry_delay_seconds,
            sandbox_config=self._sandbox_config.model_copy(deep=True),
        )
        self._codex_endpoint_backends: dict[str, CodexWebChatBackend] = {}
        for endpoint_name, endpoint in self._generation_endpoints.items():
            strategy = codex_local_transport_strategy(endpoint.protocol)
            if endpoint.wire_api == "responses" or strategy == "config-override":
                try:
                    endpoint_client = CodexAppServerClient(
                        config_overrides=codex_endpoint_config_overrides(
                            endpoint_name,
                            endpoint,
                        ),
                        env_overrides=codex_endpoint_app_server_env(endpoint_name, endpoint),
                    )
                except ValueError:
                    continue
            elif strategy == "oss":
                endpoint_client = CodexAppServerClient(
                    global_args=codex_oss_launch_args(
                        codex_oss_provider_for_local_endpoint(endpoint)
                    )
                )
            else:
                continue
            self._codex_endpoint_backends[endpoint_name] = CodexWebChatBackend(
                client=endpoint_client,
                generation_endpoint=endpoint,
                transcript_retry_attempts=codex_transcript_retry_attempts,
                transcript_retry_delay_seconds=codex_transcript_retry_delay_seconds,
                sandbox_config=self._sandbox_config.model_copy(deep=True),
            )
        self._grok_backend = GrokWebChatBackend(
            sandbox_config=self._sandbox_config.model_copy(deep=True)
        )
        self._qwen_backend = QwenWebChatBackend(
            sandbox_config=self._sandbox_config.model_copy(deep=True),
            local_generation_endpoints={
                name: endpoint
                for name, endpoint in self._generation_endpoints.items()
                if endpoint.wire_api == "chat-completions"
            },
        )
        self._droid_backend = DroidWebChatBackend(
            sandbox_config=self._sandbox_config.model_copy(deep=True)
        )
        # Discovered ACP SessionInfo payloads keyed by (provider, sessionId).
        # The ACP lifecycle service (discovery) populates this; readers use it to
        # reconcile agent-side sessions against canonical rows.
        self._acp_session_infos: dict[tuple[str, str], dict[str, Any]] = {}

    @property
    def sandbox_config(self) -> SandboxConfig:
        """Return current daemon-owned web-chat sandbox config."""
        return self._refresh_sandbox_config().model_copy(deep=True)

    @property
    def sandbox_policy_hash(self) -> str:
        """Return current web-chat sandbox policy hash."""
        self._refresh_sandbox_config()
        return self._sandbox_policy_hash

    def policy_mismatch_reason(self, session: Any) -> str | None:
        """Return a user-facing reason when a web-chat session cannot be resumed."""
        if getattr(session, "session_type", None) != "web_chat":
            return None

        stored_policy_hash = getattr(session, "sandbox_policy_hash", None)
        if (
            isinstance(stored_policy_hash, str)
            and stored_policy_hash
            and stored_policy_hash != self.sandbox_policy_hash
        ):
            return web_chat_policy_mismatch_message()

        return None

    @property
    def codex_client(self) -> CodexAppServerClient | None:
        """Expose the shared Codex app-server client."""
        return self._codex_backend.client

    async def start(self, *, background: bool = False) -> None:
        """Start daemon-owned provider backends."""
        if background:
            await self._codex_backend.start(background=True)
            for backend in self._codex_endpoint_backends.values():
                await backend.start(background=True)
            await self._droid_backend.start(background=True)
            return

        await self._codex_backend.start()
        for backend in self._codex_endpoint_backends.values():
            await backend.start()
        await self._grok_backend.start()
        await self._qwen_backend.start()
        await self._droid_backend.start()

    async def stop(self) -> None:
        """Stop daemon-owned provider backends."""
        await self._droid_backend.stop()
        await self._qwen_backend.stop()
        await self._grok_backend.stop()
        for backend in self._codex_endpoint_backends.values():
            await backend.stop()
        await self._codex_backend.stop()

    def health(self, provider: str) -> ProviderBackendHealth:
        """Return provider backend health for picker availability and status."""
        if provider == "codex":
            return self._codex_backend.health()
        if provider == "grok":
            return self._grok_backend.health()
        if provider == "qwen":
            return self._qwen_backend.health()
        if provider == "agy":
            from gobby.providers.version_gate import peek_agy_support

            record = peek_agy_support()
            return ProviderBackendHealth(
                provider=provider,
                available=False,
                startup_error=(record.reason if not record.supported else AGY_UNAVAILABLE_REASON),
            )
        if provider == "droid":
            return self._droid_backend.health()
        if provider == "claude":
            return self._claude_backend.health()
        try:
            endpoint_name = parse_endpoint_selector(provider)
        except ValueError as exc:
            return ProviderBackendHealth(provider=provider, available=False, startup_error=str(exc))
        if endpoint_name is not None:
            backend = self._codex_endpoint_backends.get(endpoint_name)
            if backend is not None:
                inner = backend.health()
                return ProviderBackendHealth(
                    provider=provider,
                    available=inner.available,
                    startup_error=inner.startup_error,
                )
            if endpoint_name in self._generation_endpoints:
                return ProviderBackendHealth(
                    provider=provider,
                    available=False,
                    startup_error="no web-chat transport for this generation endpoint",
                )
        return ProviderBackendHealth(provider=provider, available=False, startup_error="unknown")

    def health_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return all provider health states as plain dicts."""
        return {
            "claude": self.health("claude").to_dict(),
            "grok": self.health("grok").to_dict(),
            "qwen": self.health("qwen").to_dict(),
            "agy": self.health("agy").to_dict(),
            "codex": self.health("codex").to_dict(),
            "droid": self.health("droid").to_dict(),
        }

    def acp_backends(self) -> dict[str, ACPWebChatBackend]:
        """Return ACP-routed provider backends keyed by provider.

        Only Grok and Qwen route through the ACP backend; there is no literal
        ``"acp"`` provider.
        """
        return {"grok": self._grok_backend, "qwen": self._qwen_backend}

    def acp_backend(self, provider: str) -> ACPWebChatBackend | None:
        """Return the daemon-owned ACP backend for a provider, or ``None``."""
        return self.acp_backends().get(provider)

    def acp_session_capabilities(self, provider: str) -> dict[str, bool]:
        """Return ACP session lifecycle capabilities for a provider (empty when N/A)."""
        backend = self.acp_backend(provider)
        if backend is None:
            return {}
        return backend.session_capabilities

    def cache_acp_session_info(self, provider: str, session_id: str, info: dict[str, Any]) -> None:
        """Cache a discovered ACP ``SessionInfo`` keyed by ``(provider, sessionId)``."""
        self._acp_session_infos[(provider, session_id)] = dict(info)

    def get_acp_session_info(self, provider: str, session_id: str) -> dict[str, Any] | None:
        """Return a cached ACP ``SessionInfo`` copy, or ``None`` when absent."""
        cached = self._acp_session_infos.get((provider, session_id))
        return dict(cached) if cached is not None else None

    def acp_session_infos(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Return a copy of the discovered ACP ``SessionInfo`` cache."""
        return {key: dict(value) for key, value in self._acp_session_infos.items()}

    def create_session(
        self,
        *,
        provider: str,
        conversation_id: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ChatSessionProtocol:
        """Create a provider-specific session wrapper for web chat."""
        self._refresh_sandbox_config()
        if provider not in {"claude", "codex", "droid", "grok", "qwen", "agy"}:
            raise RuntimeError(f"Unsupported web chat provider: {provider}")
        if provider == "agy":
            from gobby.providers.version_gate import peek_agy_support

            record = peek_agy_support()
            raise RuntimeError(record.reason if not record.supported else AGY_UNAVAILABLE_REASON)
        if provider == "qwen":
            return QwenManagedChatSession(
                conversation_id=conversation_id,
                _backend=self._qwen_backend,
                _model=model,
                reasoning_effort=reasoning_effort,
            )
        if provider == "grok":
            return GrokManagedChatSession(
                conversation_id=conversation_id,
                _backend=self._grok_backend,
                _model=model,
                reasoning_effort=reasoning_effort,
            )
        if provider == "codex":
            codex_backend, resolved_model = self._codex_backend_for_model(model)
            return CodexManagedChatSession(
                conversation_id=conversation_id,
                _backend=codex_backend,
                _model=resolved_model,
                _model_selector=model if parse_endpoint_model_selector(model) is not None else None,
                reasoning_effort=reasoning_effort,
                _transcript_retry_attempts=codex_backend.transcript_retry_attempts,
                _transcript_retry_delay_seconds=codex_backend.transcript_retry_delay_seconds,
            )
        if provider == "droid":
            return DroidManagedChatSession(
                conversation_id=conversation_id,
                _backend=self._droid_backend,
                _model=model,
                reasoning_effort=reasoning_effort,
            )
        if provider != "claude":
            raise RuntimeError(f"Unsupported web chat provider: {provider}")

        session: ChatSessionProtocol = self._claude_backend.create_session(conversation_id)
        if isinstance(session, ChatSession) and model:
            session._model = model
        if isinstance(session, ChatSession):
            session.reasoning_effort = reasoning_effort
        return session

    def _refresh_sandbox_config(self) -> SandboxConfig:
        if self._config_resolver is None:
            return self._sandbox_config
        daemon_config = self._config_resolver()
        if daemon_config is None:
            return self._sandbox_config
        self._sandbox_config = web_chat_sandbox_config(daemon_config)
        self._sandbox_policy_hash = web_chat_sandbox_policy_hash(daemon_config)
        self._claude_backend.set_sandbox_config(self._sandbox_config)
        self._codex_backend.set_sandbox_config(self._sandbox_config)
        for backend in self._codex_endpoint_backends.values():
            backend.set_sandbox_config(self._sandbox_config)
        self._grok_backend.set_sandbox_config(self._sandbox_config)
        self._qwen_backend.set_sandbox_config(self._sandbox_config)
        self._droid_backend.set_sandbox_config(self._sandbox_config)
        return self._sandbox_config

    def _codex_backend_for_model(self, model: str | None) -> tuple[CodexWebChatBackend, str | None]:
        selector = parse_endpoint_model_selector(model)
        if selector is None:
            return self._codex_backend, model
        endpoint = self._generation_endpoints.get(selector.endpoint_name)
        if endpoint is None:
            raise RuntimeError(f"Unknown generation endpoint: {selector.endpoint_name}")
        backend = self._codex_endpoint_backends.get(selector.endpoint_name)
        if backend is None:
            if endpoint.wire_api == "responses":
                raise RuntimeError(
                    f"Codex Responses endpoint {selector.endpoint_name!r} is unavailable"
                )
            if codex_local_transport_strategy(endpoint.protocol) == "config-override":
                raise RuntimeError(f"vLLM endpoint {selector.endpoint_name!r} is unavailable")
            raise RuntimeError(
                f"Codex OSS local web chat supports {codex_oss_supported_provider_clause()}"
            )
        return backend, selector.model or endpoint.model
