"""Cross-language golden serialization vectors for the v2 grant bundle.

Schema identity is signed. After a migration changes
``expected_schema_identity()``, regenerate every file in ``golden/``:
recompute each ``payload_checksum`` and re-sign the payloads with
``GOLDEN_SECRET`` from ``tests/runtime_grants/support.py``.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path

import pytest
from pydantic import ValidationError

from gobby.runtime_grants import (
    DeploymentGrantContext,
    GrantBundle,
    GrantService,
    WrongApiContractGrant,
    canonical_payload_bytes,
    payload_checksum,
    sign_grant,
)
from gobby.runtime_grants.schema import API_CONTRACT, PostgresDirect
from gobby.runtime_grants.signing import signature_matches
from gobby.storage.schema_contract import expected_schema_identity
from tests.runtime_grants.support import (
    DEPLOYMENT_TOKEN,
    FENCING_EPOCH,
    GOLDEN_SECRET,
    StaticRuntime,
    revision_snapshot,
)

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
NEGATIVE_GOLDENS = frozenset({"payload_skew_unknown_field.json"})

REQUIRED_MODES: dict[str, frozenset[str]] = {
    "postgres": frozenset({"direct", "brokered", "unavailable"}),
    "falkordb": frozenset({"direct", "brokered", "unavailable"}),
    "qdrant": frozenset({"direct", "brokered", "unavailable"}),
    "embed": frozenset({"daemon", "unavailable"}),
    "text_generate": frozenset({"daemon", "unavailable"}),
    "tool_chat": frozenset({"daemon", "unavailable"}),
    "vision_extract": frozenset({"daemon", "unavailable"}),
    "audio_transcribe": frozenset({"daemon", "unavailable"}),
}


def _golden_paths() -> list[Path]:
    return sorted(path for path in GOLDEN_DIR.glob("*.json") if path.name not in NEGATIVE_GOLDENS)


def _load_grants() -> list[tuple[Path, bytes, GrantBundle]]:
    loaded: list[tuple[Path, bytes, GrantBundle]] = []
    for path in _golden_paths():
        raw = path.read_bytes()
        loaded.append((path, raw, GrantBundle.model_validate_json(raw)))
    return loaded


@pytest.mark.unit
def test_grant_vectors_round_trip() -> None:
    loaded = _load_grants()
    assert loaded, f"expected golden vectors under {GOLDEN_DIR}"

    found: dict[str, set[str]] = defaultdict(set)
    for path, raw, grant in loaded:
        assert grant.model_dump_canonical() == raw, path.name
        found["postgres"].add(grant.capabilities.postgres.mode)
        found["falkordb"].add(grant.capabilities.falkordb.mode)
        found["qdrant"].add(grant.capabilities.qdrant.mode)
        found["embed"].add(grant.capabilities.embed.mode)
        found["text_generate"].add(grant.capabilities.text_generate.mode)
        found["tool_chat"].add(grant.capabilities.tool_chat.mode)
        found["vision_extract"].add(grant.capabilities.vision_extract.mode)
        found["audio_transcribe"].add(grant.capabilities.audio_transcribe.mode)

    for name, required in REQUIRED_MODES.items():
        assert required <= found[name], f"{name} missing variants {required - found[name]}"


@pytest.mark.unit
def test_config_revision_signed() -> None:
    loaded = _load_grants()
    assert loaded, f"expected golden vectors under {GOLDEN_DIR}"
    identity = expected_schema_identity()
    for path, _raw, grant in loaded:
        assert isinstance(grant.config_revision, int), path.name
        payload = grant.model_dump(mode="json", exclude={"payload_checksum", "signature"})
        assert "config_revision" in payload
        assert payload["config_revision"] == grant.config_revision
        assert grant.schema_identity.model_dump(mode="json") == {
            "runner_protocol": identity["runner_protocol"],
            "baseline_version": identity["baseline_version"],
            "baseline_checksum": identity["baseline_checksum"],
            "latest_version": identity["latest_version"],
            "latest_checksum": identity["latest_checksum"],
            "assets_root_hash": identity["assets_root_hash"],
        }
        assert signature_matches(grant, GOLDEN_SECRET)
        mutated = grant.model_copy(update={"config_revision": grant.config_revision + 1})
        assert payload_checksum(mutated) != grant.payload_checksum
        assert sign_grant(mutated, GOLDEN_SECRET).signature != grant.signature


@pytest.mark.unit
def test_present_old_client_new_grant_rejects_api_contract() -> None:
    path = GOLDEN_DIR / "old_client_new_grant.json"
    grant = GrantBundle.model_validate_json(path.read_bytes())
    assert grant.api_contract != API_CONTRACT
    service = GrantService(
        runtime=StaticRuntime(
            revision_snapshot(
                41,
                host="falkor-a.test",
                password="falkor-secret-a",
                qdrant_url="http://qdrant-a.test:6333",
                api_key="qdrant-secret-a",
            )
        ),
        context=DeploymentGrantContext(
            token=DEPLOYMENT_TOKEN,
            fencing_epoch=FENCING_EPOCH,
            signing_secret=GOLDEN_SECRET,
        ),
    )
    with pytest.raises(WrongApiContractGrant):
        service.present(grant, now=grant.issued_at + 10)


@pytest.mark.unit
def test_payload_checksum_pinned() -> None:
    loaded = _load_grants()
    assert loaded, f"expected golden vectors under {GOLDEN_DIR}"
    for path, _raw, grant in loaded:
        digest = payload_checksum(grant)
        assert grant.payload_checksum == digest, path.name
        assert digest == hashlib.sha256(canonical_payload_bytes(grant)).hexdigest()
        assert len(grant.payload_checksum) == 64
        assert len(grant.signature) == 64


# Changing this inventory requires bumping EXPECTED_API_CONTRACT (Rust) and
# API_CONTRACT (Python) together and regenerating the goldens.
GRANT_FIELD_INVENTORY: tuple[str, ...] = (
    "api_contract",
    "capabilities",
    "config_revision",
    "deployment",
    "expires_at",
    "issued_at",
    "payload_checksum",
    "postgres.credential_generation",
    "postgres.dsn",
    "postgres.mode",
    "postgres.role_name",
    "postgres.valid_until",
    "principal",
    "schema_identity",
    "signature",
    "version",
)


@pytest.mark.unit
def test_payload_skew_unknown_field_golden_rejects() -> None:
    path = GOLDEN_DIR / "payload_skew_unknown_field.json"
    raw = path.read_bytes()
    payload = json.loads(raw)
    assert payload["api_contract"] == API_CONTRACT
    assert payload["future_capability_probe"] == 1
    with pytest.raises(ValidationError) as exc_info:
        GrantBundle.model_validate_json(raw)
    extras = [err for err in exc_info.value.errors() if err["type"] == "extra_forbidden"]
    assert extras, exc_info.value.errors()
    assert any(err["loc"] == ("future_capability_probe",) for err in extras)


@pytest.mark.unit
def test_grant_field_inventory_matches_api_contract() -> None:
    inventory = tuple(
        sorted(
            [
                *GrantBundle.model_fields,
                *(f"postgres.{name}" for name in PostgresDirect.model_fields),
            ]
        )
    )
    assert inventory == GRANT_FIELD_INVENTORY
