"""Flat config-value cases that pin the native capability port to Python.

Each case is the flat ``active_values`` a published config row carries, plus the
plaintext of any active secret binding. ``snapshot_for`` rebuilds the typed
snapshot Python reads, so the expected capabilities come from
``capabilities_from_snapshot`` itself.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any

from gobby.config.app import DaemonConfig
from gobby.config.runtime_models import ConfigSnapshot
from gobby.runtime_grants.schema import PostgresDirect
from gobby.runtime_grants.service import capabilities_from_snapshot
from tests.runtime_grants.support import config_snapshot, daemon_config

REVISION = 41

POSTGRES = PostgresDirect(
    dsn="postgresql://gobby_grant_fixture:fixture@127.0.0.1:5432/gobby?sslmode=disable",
    role_name="gobby_grant_fixture",
    credential_generation=3,
    valid_until=4102444800,
)

BASE_VALUES: dict[str, object] = {
    "ai.embeddings.model": "nomic-embed-text",
    "databases.falkordb.host": "localhost",
    "databases.falkordb.port": 6379,
    "databases.falkordb.password": None,
    "databases.qdrant.url": "http://127.0.0.1:6333",
    "databases.qdrant.api_key": None,
    "voice.enabled": False,
    "voice.stt_enabled": False,
    "voice.openai_compatible_audio": [],
}

FALKOR_SECRET = "databases.falkordb.password"
QDRANT_SECRET = "databases.qdrant.api_key"

ENDPOINT_TEXT_ONLY: dict[str, object] = {
    "ai.generation.endpoints.text.api_base": "http://gen.example.test/v1",
    "ai.generation.endpoints.text.model": "text-model",
    "ai.generation.endpoints.text.input_modalities": ["text"],
}

ENDPOINT_VISION: dict[str, object] = {
    "ai.generation.endpoints.vision.api_base": "http://gen.example.test/v1",
    "ai.generation.endpoints.vision.model": "vision-model",
    "ai.generation.endpoints.vision.input_modalities": ["text", "image"],
}

AUDIO_BINDING: dict[str, object] = {
    "provider": "audio-fixture",
    "url": "http://audio.example.test/v1",
    "model": "whisper-1",
    "api_key": None,
    "transcription_enabled": True,
}


def _case(
    overrides: Mapping[str, object] | None = None,
    secrets: Mapping[str, str | None] | None = None,
) -> tuple[dict[str, object], dict[str, str | None]]:
    values = {**BASE_VALUES, **(overrides or {})}
    return values, dict(secrets or {})


CASES: tuple[tuple[str, dict[str, object], dict[str, str | None]], ...] = (
    ("loopback_direct", *_case()),
    (
        "remote_falkor_without_password_brokers",
        *_case({"databases.falkordb.host": "falkor.remote.example"}),
    ),
    (
        "falkor_secret_binding_direct",
        *_case(
            {
                "databases.falkordb.host": "falkor.remote.example",
                FALKOR_SECRET: f"$secret:{FALKOR_SECRET}",
            },
            {FALKOR_SECRET: "falkor-secret"},
        ),
    ),
    (
        "falkor_unresolved_password_brokers",
        *_case(
            {
                "databases.falkordb.host": "falkor.remote.example",
                FALKOR_SECRET: f"$secret:{FALKOR_SECRET}",
            },
            {FALKOR_SECRET: None},
        ),
    ),
    ("falkor_unresolved_host_unavailable", *_case({"databases.falkordb.host": "${FALKOR_HOST}"})),
    (
        "qdrant_remote_with_key_direct",
        *_case({"databases.qdrant.url": "https://qdrant.example.com", QDRANT_SECRET: "qdrant-key"}),
    ),
    (
        "qdrant_remote_without_key_brokers",
        *_case({"databases.qdrant.url": "https://qdrant.example.com"}),
    ),
    ("qdrant_missing_url_unavailable", *_case({"databases.qdrant.url": None})),
    ("qdrant_ipv6_loopback_direct", *_case({"databases.qdrant.url": "http://[::1]:6333"})),
    ("embed_unset_unavailable", *_case({"ai.embeddings.model": ""})),
    (
        "vision_and_voice_stt_enabled",
        *_case(
            {
                **ENDPOINT_VISION,
                "voice.enabled": True,
                "voice.stt_enabled": True,
            }
        ),
    ),
    (
        "audio_from_openai_compatible_binding",
        *_case({"voice.openai_compatible_audio": [AUDIO_BINDING]}),
    ),
    (
        "stt_flag_without_voice_enabled_is_unavailable",
        *_case(
            {
                **ENDPOINT_TEXT_ONLY,
                "voice.enabled": False,
                "voice.stt_enabled": True,
            }
        ),
    ),
    (
        "remote_falkor_and_qdrant_broker_operations",
        *_case(
            {
                "databases.falkordb.host": "falkor.remote.example",
                "databases.qdrant.url": "https://qdrant.example.com",
            }
        ),
    ),
)


def _typed_path(key: str) -> str:
    """Map a flat registry key to the typed DaemonConfig path it is stored under."""
    if key.startswith("ai.embeddings."):
        return "embeddings." + key.removeprefix("ai.embeddings.")
    return key


def _nest(values: Mapping[str, object]) -> dict[str, Any]:
    nested: dict[str, Any] = {}
    for key, value in values.items():
        *parents, leaf = _typed_path(key).split(".")
        current = nested
        for segment in parents:
            current = current.setdefault(segment, {})
        current[leaf] = value
    return nested


def snapshot_for(values: Mapping[str, object], secrets: Mapping[str, str | None]) -> ConfigSnapshot:
    """Rebuild the typed snapshot Python would hold for these flat active values."""
    document = daemon_config().model_dump(mode="json", by_alias=True)
    merged = _merge(document, _nest(values))
    config = DaemonConfig.model_validate(merged)
    return config_snapshot(config, revision=REVISION, active_secrets=secrets)


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def build_document() -> dict[str, Any]:
    """Generate the committed fixture from Python's capabilities_from_snapshot."""
    cases = []
    for name, values, secrets in CASES:
        capabilities = capabilities_from_snapshot(snapshot_for(values, secrets), POSTGRES)
        cases.append(
            {
                "name": name,
                "values": values,
                "secrets": secrets,
                "expected": capabilities.model_dump(mode="json"),
            }
        )
    return {"postgres": POSTGRES.model_dump(mode="json"), "cases": cases}


if __name__ == "__main__":
    json.dump(build_document(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
