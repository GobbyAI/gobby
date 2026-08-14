"""Shared builders for v2 grant tests."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Protocol

from gobby.config.app import DaemonConfig
from gobby.config.runtime import RuntimeActiveBundle
from gobby.config.runtime_models import ApplyFailure, ConfigSnapshot, RuntimeSecretBinding

GOLDEN_SECRET = "golden-grant-signing-secret"
DEPLOYMENT_TOKEN = "cafebabedeadbeef"
FENCING_EPOCH = 7


class CaptureRuntime(Protocol):
    def capture(self) -> RuntimeActiveBundle: ...


class StaticRuntime:
    def __init__(self, snapshot: ConfigSnapshot) -> None:
        self._bundle = RuntimeActiveBundle(snapshot=snapshot, services=MappingProxyType({}))

    def capture(self) -> RuntimeActiveBundle:
        return self._bundle

    @property
    def snapshot(self) -> ConfigSnapshot:
        return self._bundle.snapshot


class SuccessiveCaptureRuntime:
    """Returns a different bundle on each capture(); snapshot tracks the latest."""

    def __init__(self, first: ConfigSnapshot, second: ConfigSnapshot) -> None:
        self._first = first
        self._second = second
        self.capture_count = 0

    def capture(self) -> RuntimeActiveBundle:
        self.capture_count += 1
        snapshot = self._first if self.capture_count == 1 else self._second
        return RuntimeActiveBundle(snapshot=snapshot, services=MappingProxyType({}))

    @property
    def snapshot(self) -> ConfigSnapshot:
        return self._second if self.capture_count else self._first


class SnapshotAfterCaptureRuntime:
    """capture() stays on the first snapshot; snapshot property flips afterward."""

    def __init__(self, first: ConfigSnapshot, second: ConfigSnapshot) -> None:
        self._first = first
        self._second = second
        self._captured = False

    def capture(self) -> RuntimeActiveBundle:
        self._captured = True
        return RuntimeActiveBundle(snapshot=self._first, services=MappingProxyType({}))

    def publish_second(self) -> None:
        self._captured = True

    @property
    def snapshot(self) -> ConfigSnapshot:
        return self._second if self._captured else self._first


def daemon_config(
    *,
    falkor_host: str = "falkor-a.test",
    falkor_port: int = 6379,
    falkor_password: str | None = "falkor-secret-a",
    qdrant_url: str = "http://qdrant-a.test:6333",
    qdrant_api_key: str | None = "qdrant-secret-a",
    embedding_model: str = "daemon-embed",
) -> DaemonConfig:
    return DaemonConfig(
        database_url="postgresql://daemon:gobby@127.0.0.1:5432/gobby",
        embeddings={
            "model": embedding_model,
            "dim": 768,
            "api_base": "http://embed.test/v1",
            "api_key": "embed-key",
            "query_prefix": None,
            "catalog_key": "catalog",
        },
        databases={
            "falkordb": {
                "host": falkor_host,
                "port": falkor_port,
                "password": falkor_password,
            },
            "qdrant": {
                "url": qdrant_url,
                "api_key": qdrant_api_key,
            },
        },
    )


def config_snapshot(
    config: DaemonConfig,
    *,
    revision: int,
    active_secrets: Mapping[str, str | None] | None = None,
    desired_secrets: Mapping[str, str | None] | None = None,
    failed_live_keys: Mapping[str, ApplyFailure] | None = None,
) -> ConfigSnapshot:
    values: dict[str, object] = {
        "databases.falkordb.host": config.databases.falkordb.host,
        "databases.falkordb.port": config.databases.falkordb.port,
        "databases.qdrant.url": config.databases.qdrant.url,
        "ai.embeddings.model": config.embeddings.model,
    }
    active_bindings = {
        key: RuntimeSecretBinding(
            reference=f"$secret:{key}", plaintext=value, fingerprint=f"fp-{key}"
        )
        for key, value in (active_secrets or {}).items()
    }
    desired_bindings = {
        key: RuntimeSecretBinding(
            reference=f"$secret:{key}", plaintext=value, fingerprint=f"fp-d-{key}"
        )
        for key, value in (desired_secrets or {}).items()
    }
    return ConfigSnapshot(
        revision=revision,
        desired=config,
        active=config,
        row_revisions=dict.fromkeys(values, revision),
        pending_restart_keys=frozenset(),
        failed_live_keys=dict(failed_live_keys or {}),
        desired_values=values,
        active_values=values,
        desired_bindings=desired_bindings,
        active_bindings=active_bindings,
    )


def revision_snapshot(
    revision: int,
    *,
    host: str,
    password: str | None,
    qdrant_url: str,
    api_key: str | None,
    failed_rotation: bool = False,
    desired_password: str | None = None,
) -> ConfigSnapshot:
    config = daemon_config(
        falkor_host=host,
        falkor_password="$secret:databases.falkordb.password"
        if password or failed_rotation
        else None,
        qdrant_url=qdrant_url,
        qdrant_api_key="$secret:databases.qdrant.api_key" if api_key else None,
    )
    failures: dict[str, ApplyFailure] = {}
    if failed_rotation:
        failures["databases.falkordb.password"] = ApplyFailure(
            revision=revision,
            subscriber="secrets",
            keys=frozenset({"databases.falkordb.password"}),
            message="rotation failed",
        )
    return config_snapshot(
        config,
        revision=revision,
        active_secrets={
            "databases.falkordb.password": password,
            "databases.qdrant.api_key": api_key,
        },
        desired_secrets={
            "databases.falkordb.password": desired_password
            if desired_password is not None
            else password,
            "databases.qdrant.api_key": api_key,
        },
        failed_live_keys=failures,
    )
