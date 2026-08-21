"""Explicit persisted-state snapshot for idempotent full installs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import click

from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_BASE_KEY,
    AI_EMBEDDING_API_KEY_KEY,
    AI_EMBEDDING_DIM_KEY,
    AI_EMBEDDING_MODEL_KEY,
)
from gobby.storage.config_mutations import config_key_to_secret_name
from gobby.storage.config_store import ConfigStore
from gobby.storage.secrets import SecretStore
from gobby.utils.deps import fingerprint_embedding_server_sync


@dataclass(frozen=True)
class InstallSectionState:
    configured: bool
    summary: str
    problem: str | None = None


@dataclass(frozen=True)
class EmbeddingInstallState(InstallSectionState):
    provider: str = "none"
    model: str | None = None
    api_base: str | None = None
    dim: int | None = None
    has_api_key: bool = False


@dataclass(frozen=True)
class VoiceInstallState(InstallSectionState):
    enabled: bool | None = None


@dataclass(frozen=True)
class InstallState:
    embedding: EmbeddingInstallState
    voice: VoiceInstallState
    qdrant: InstallSectionState
    falkordb: InstallSectionState
    has_existing_values: bool

    def validate(self) -> None:
        for label, section in (
            ("Embedding", self.embedding),
            ("Voice", self.voice),
            ("Qdrant", self.qdrant),
            ("FalkorDB", self.falkordb),
        ):
            if section.problem:
                raise click.ClickException(f"{label} install state is invalid: {section.problem}")


def snapshot_install_state(config_store: ConfigStore, secret_store: SecretStore) -> InstallState:
    """Read only explicit installer-owned keys and secret presence."""
    values = config_store.read_snapshot().overrides
    keys = set(values)
    managed_keys = {
        AI_EMBEDDING_MODEL_KEY,
        AI_EMBEDDING_API_BASE_KEY,
        AI_EMBEDDING_DIM_KEY,
        AI_EMBEDDING_API_KEY_KEY,
        "voice.enabled",
        "databases.qdrant.url",
        "databases.qdrant.port",
        "databases.falkordb.host",
        "databases.falkordb.port",
        "databases.falkordb.password",
    }
    return InstallState(
        embedding=_embedding_state(values, secret_store, keys),
        voice=_voice_state(values, keys),
        qdrant=_qdrant_state(values, keys),
        falkordb=_falkordb_state(values, secret_store, keys),
        has_existing_values=bool(keys & managed_keys),
    )


def empty_install_state() -> InstallState:
    return InstallState(
        embedding=EmbeddingInstallState(configured=False, summary="not configured"),
        voice=VoiceInstallState(configured=False, summary="not configured"),
        qdrant=InstallSectionState(configured=False, summary="not configured"),
        falkordb=InstallSectionState(configured=False, summary="not configured"),
        has_existing_values=False,
    )


def prepare_install_state(
    config_store: ConfigStore | None,
    secret_store: SecretStore | None,
) -> InstallState:
    if config_store is None or secret_store is None:
        raise click.ClickException(
            "Cannot inspect existing install state because the PostgreSQL hub is unavailable; "
            "repair bootstrap.yaml/database_url and retry"
        )
    state = snapshot_install_state(config_store, secret_store)
    state.validate()
    if state.has_existing_values:
        echo_install_state(state)
    return state


def echo_install_state(state: InstallState) -> None:
    click.echo("Current optional-service configuration:")
    click.echo(f"  Embedding: {state.embedding.summary}")
    click.echo(f"  Voice: {state.voice.summary}")
    click.echo(f"  Qdrant: {state.qdrant.summary}")
    click.echo(f"  FalkorDB: {state.falkordb.summary}")
    click.echo("")


def should_configure_section(
    section: InstallSectionState,
    *,
    label: str,
    no_interactive: bool,
    explicit: bool = False,
) -> bool:
    """Return whether setup should run, defaulting configured sections to keep."""
    if explicit or not section.configured:
        return True
    if no_interactive:
        return False
    try:
        return click.confirm(f"Change {label}?", default=False)
    except (click.Abort, EOFError):
        click.echo("")
        return False


def _embedding_state(
    values: Mapping[str, object],
    secrets: SecretStore,
    keys: set[str],
) -> EmbeddingInstallState:
    relevant = {
        AI_EMBEDDING_MODEL_KEY,
        AI_EMBEDDING_API_BASE_KEY,
        AI_EMBEDDING_DIM_KEY,
        AI_EMBEDDING_API_KEY_KEY,
    }
    present = keys & relevant
    if not present:
        return EmbeddingInstallState(configured=False, summary="not configured")

    model = values.get(AI_EMBEDDING_MODEL_KEY)
    api_base = values.get(AI_EMBEDDING_API_BASE_KEY)
    dim = values.get(AI_EMBEDDING_DIM_KEY)
    model_value = model.strip() if isinstance(model, str) and model.strip() else None
    api_base_value = api_base.strip() if isinstance(api_base, str) and api_base.strip() else None
    dim_value = _optional_int(dim)
    has_api_key = _secret_present(values, secrets, AI_EMBEDDING_API_KEY_KEY, keys)

    disabled = (
        AI_EMBEDDING_MODEL_KEY in keys
        and AI_EMBEDDING_DIM_KEY in keys
        and model_value is None
        and dim_value == 0
    )
    if disabled:
        return EmbeddingInstallState(
            configured=True,
            summary="disabled",
            provider="none",
            dim=0,
            has_api_key=has_api_key,
        )

    required = {AI_EMBEDDING_MODEL_KEY, AI_EMBEDDING_DIM_KEY}
    if not required <= keys or model_value is None or dim_value is None or dim_value <= 0:
        return EmbeddingInstallState(
            configured=False,
            summary="incomplete",
            problem="model and a positive dimension must both be set",
        )

    provider = _embedding_provider(api_base_value, has_api_key)
    if provider == "openai" and not has_api_key:
        return EmbeddingInstallState(
            configured=False,
            summary="incomplete OpenAI configuration",
            problem="OpenAI embeddings require the canonical embedding SecretStore entry",
            provider=provider,
            model=model_value,
            api_base=api_base_value,
            dim=dim_value,
        )
    endpoint = api_base_value or "provider default"
    return EmbeddingInstallState(
        configured=True,
        summary=f"{provider} / {model_value} / {endpoint}",
        provider=provider,
        model=model_value,
        api_base=api_base_value,
        dim=dim_value,
        has_api_key=has_api_key,
    )


def _voice_state(values: Mapping[str, object], keys: set[str]) -> VoiceInstallState:
    if "voice.enabled" not in keys:
        return VoiceInstallState(configured=False, summary="not configured")
    value = values.get("voice.enabled")
    if not isinstance(value, bool):
        return VoiceInstallState(
            configured=False,
            summary="invalid",
            problem="voice.enabled must be true or false",
        )
    return VoiceInstallState(
        configured=True,
        summary="enabled" if value else "disabled",
        enabled=value,
    )


def _qdrant_state(values: Mapping[str, object], keys: set[str]) -> InstallSectionState:
    required = {"databases.qdrant.url", "databases.qdrant.port"}
    present = keys & required
    if not present:
        return InstallSectionState(configured=False, summary="not configured")
    url = values.get("databases.qdrant.url")
    port = _optional_int(values.get("databases.qdrant.port"))
    if present != required or not isinstance(url, str) or not url.strip() or not _valid_port(port):
        return InstallSectionState(
            configured=False,
            summary="incomplete",
            problem="databases.qdrant.url and a valid databases.qdrant.port must both be set",
        )
    return InstallSectionState(configured=True, summary=f"{url.strip()} (port {port})")


def _falkordb_state(
    values: Mapping[str, object],
    secrets: SecretStore,
    keys: set[str],
) -> InstallSectionState:
    required = {
        "databases.falkordb.host",
        "databases.falkordb.port",
        "databases.falkordb.password",
    }
    present = keys & required
    if not present:
        return InstallSectionState(configured=False, summary="not configured")
    host = values.get("databases.falkordb.host")
    port = _optional_int(values.get("databases.falkordb.port"))
    has_password = _secret_present(values, secrets, "databases.falkordb.password", keys)
    if (
        present != required
        or not isinstance(host, str)
        or not host.strip()
        or not _valid_port(port)
        or not has_password
    ):
        return InstallSectionState(
            configured=False,
            summary="incomplete",
            problem="host, port, and the canonical SecretStore password must all be set",
        )
    return InstallSectionState(
        configured=True, summary=f"{host.strip()}:{port} (credential stored)"
    )


def _secret_present(
    values: Mapping[str, object],
    secrets: SecretStore,
    key: str,
    keys: set[str],
) -> bool:
    if key not in keys:
        return False
    configured = values.get(key)
    if not isinstance(configured, str) or not configured.startswith("$secret:"):
        return False
    secret_name = configured.removeprefix("$secret:") or config_key_to_secret_name(key)
    if not secret_name:
        return False
    return secrets.exists(secret_name)


def _embedding_provider(api_base: str | None, has_api_key: bool) -> str:
    if api_base:
        if "api.openai.com" in api_base.lower():
            return "openai"
        fingerprinted = fingerprint_embedding_server_sync(api_base)
        if fingerprinted is not None:
            return fingerprinted
        return "openai-compatible"
    return "openai" if has_api_key else "openai-compatible"


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _valid_port(port: int | None) -> bool:
    return port is not None and 1 <= port <= 65535
