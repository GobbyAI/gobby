"""Codex OSS local-provider helpers."""

from __future__ import annotations

from typing import Literal, Protocol

CODEX_OSS_LOCAL_PROVIDERS = frozenset({"lmstudio", "ollama"})
CodexLocalTransportStrategy = Literal["oss", "config-override"]


class CodexOSSLocalEndpoint(Protocol):
    @property
    def protocol(self) -> str | None:
        """Return the configured endpoint protocol name."""
        ...


def codex_oss_supported_provider_clause() -> str:
    """Return the supported provider clause used in Codex OSS error messages."""
    return " or ".join(f"provider={provider}" for provider in sorted(CODEX_OSS_LOCAL_PROVIDERS))


def codex_local_transport_strategy(
    protocol: str | None,
) -> CodexLocalTransportStrategy | None:
    """Return the Codex web-chat/spawn transport for a local endpoint protocol.

    LM Studio and Ollama keep ``--oss --local-provider``. vLLM uses invocation
    config overrides with ``wire_api="chat"``. Generic OpenAI-compatible
    endpoints have no Codex transport.
    """
    normalized = protocol.strip().lower() if isinstance(protocol, str) else ""
    if normalized in CODEX_OSS_LOCAL_PROVIDERS:
        return "oss"
    if normalized == "vllm":
        return "config-override"
    return None


def codex_oss_provider_for_local_endpoint(endpoint: CodexOSSLocalEndpoint) -> str:
    """Return Codex OSS local provider name for a local generation endpoint."""
    protocol = str(endpoint.protocol).strip().lower() if endpoint.protocol is not None else ""
    if not protocol:
        raise ValueError(
            f"Codex OSS local routing requires {codex_oss_supported_provider_clause()}"
        )
    if protocol not in CODEX_OSS_LOCAL_PROVIDERS:
        raise ValueError(
            f"Codex OSS local routing supports {codex_oss_supported_provider_clause()}; "
            f"got protocol={protocol}"
        )
    return protocol


def codex_oss_launch_args(oss_provider: str) -> list[str]:
    """Return Codex global CLI arguments for an OSS local provider."""
    provider = oss_provider.strip().lower()
    if provider not in CODEX_OSS_LOCAL_PROVIDERS:
        raise ValueError(f"Unsupported Codex OSS local provider: {oss_provider}")
    return ["--oss", "--local-provider", provider]
