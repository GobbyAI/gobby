"""Record and replay the HTTP contract corpus against the front-door e2e daemon."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from gobby.runtime_grants.handshake import encode_grant_header
from tests.contracts import http_corpus
from tests.e2e import conftest as e2e_fixtures
from tests.e2e import test_runtime_boundary as boundary_fixtures
from tests.e2e.conftest import daemon_auth_headers, daemon_token
from tests.e2e.test_runtime_boundary import E2E_MACHINE_ID, BoundaryHarness, _seed_identity_rows

daemon_instance = e2e_fixtures.daemon_instance
e2e_config = e2e_fixtures.e2e_config
e2e_home_dir = e2e_fixtures.e2e_home_dir
e2e_project_dir = e2e_fixtures.e2e_project_dir
boundary = boundary_fixtures.boundary

RECORD_ENV = "GOBBY_RECORD_HTTP_CONTRACTS"
# A deployment token is 16 hex digits; a repeated digit keeps secret scanners quiet.
SYNTHETIC_DEPLOYMENT_TOKEN = "f" * 16
PYTHON_CASES = http_corpus.load_cases()


@pytest.fixture
def e2e_pre_daemon_setup(postgres_db: Any, postgres_schema: str) -> None:
    """Admit the e2e machine for the operator so the handshake can issue a grant."""
    from tests.fixtures.postgres import (
        TEST_USER_EMAIL,
        TEST_USER_ID,
        TEST_USER_NAME,
        TEST_USER_PASSWORD_HASH,
    )

    if not postgres_schema.replace("_", "").isalnum():
        raise RuntimeError(f"refusing to GRANT on unexpected schema {postgres_schema!r}")
    postgres_db.execute(f"GRANT USAGE ON SCHEMA {postgres_schema} TO gobby_gcode_capability")
    postgres_db.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
        f"{postgres_schema} TO gobby_gcode_capability"
    )
    postgres_db.execute(
        """
        INSERT INTO users (id, email, name, password_hash)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (TEST_USER_ID, TEST_USER_EMAIL, TEST_USER_NAME, TEST_USER_PASSWORD_HASH),
    )
    _seed_identity_rows(postgres_db, E2E_MACHINE_ID, TEST_USER_ID)


def _credentials(harness: BoundaryHarness) -> dict[str, dict[str, str]]:
    return {
        "operator": daemon_auth_headers(harness.home),
        "grant": harness.grant_headers(),
    }


def _client(harness: BoundaryHarness) -> httpx.Client:
    """An unauthenticated client; each case's credential recipe supplies its headers."""
    return httpx.Client(base_url=harness.daemon.http_url, timeout=10.0)


def _write_corpus(
    root: Path,
    *,
    version: int,
    families: dict[str, dict[str, str]],
    cases: list[dict[str, Any]],
) -> None:
    manifest = {
        "schema_version": version,
        "cases": [f"{case['name']}.json" for case in cases],
        "families": families,
    }
    (root / http_corpus.MANIFEST_NAME).write_text(json.dumps(manifest))
    for case in cases:
        (root / f"{case['name']}.json").write_text(json.dumps(case))


def _synthetic_case(
    name: str, family: str, version: int = 1, backend: str = "up"
) -> dict[str, Any]:
    return {
        "schema_version": version,
        "name": name,
        "family": family,
        "backend": backend,
        "credential": "none",
        "request": {"method": "GET", "path": "/", "query": {}, "headers": {}, "body": None},
        "response": {"status": 200, "headers": {}, "body": None},
        "mask": [],
    }


SYNTHETIC_FAMILIES = {
    "health": {"parity": "native", "origin": "python"},
    "front_door": {"parity": "native", "origin": "gdaemon"},
}


@pytest.mark.unit
def test_loader_rejects_stale_schema_version(tmp_path: Path) -> None:
    _write_corpus(
        tmp_path,
        version=2,
        families=SYNTHETIC_FAMILIES,
        cases=[_synthetic_case("health_ok", "health", version=1)],
    )
    with pytest.raises(http_corpus.CorpusError, match="schema_version 1 differs from manifest 2"):
        http_corpus.load_cases(tmp_path)


@pytest.mark.unit
def test_loader_rejects_missing_or_invalid_backend(tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.mkdir()
    _write_corpus(
        good,
        version=1,
        families=SYNTHETIC_FAMILIES,
        cases=[
            _synthetic_case("health_ok", "health"),
            _synthetic_case("health_backend_down", "health", backend="down"),
            _synthetic_case("front_door_backend_down", "front_door", backend="down"),
            _synthetic_case("front_door_up", "front_door"),
        ],
    )
    assert [case["name"] for case in http_corpus.load_cases(good)] == ["health_ok"]

    missing = _synthetic_case("health_missing", "health")
    del missing["backend"]
    invalid = [_synthetic_case("health_sideways", "health", backend="sideways")]
    for label, value in (("null", None), ("number", 0), ("list", []), ("object", {})):
        case = _synthetic_case(f"health_{label}", "health")
        case["backend"] = value
        invalid.append(case)
    for case in (missing, *invalid):
        bad = tmp_path / case["name"]
        bad.mkdir()
        _write_corpus(bad, version=1, families=SYNTHETIC_FAMILIES, cases=[case])
        with pytest.raises(http_corpus.CorpusError, match=rf"^{case['name']}\.json: backend"):
            http_corpus.load_cases(bad)


@pytest.mark.unit
def test_mask_vector_matches_expected() -> None:
    vector = json.loads((http_corpus.CORPUS_DIR / "mask_vector.json").read_text())
    assert vector, "mask vector has no entries"
    for entry in vector:
        document = http_corpus.redact_secrets(entry["input"])
        assert http_corpus.apply_masks(document, entry["mask"]) == entry["expected"], entry


@pytest.mark.unit
def test_manifest_contract() -> None:
    manifest = http_corpus.load_manifest()
    assert manifest["schema_version"] == 2
    assert manifest["families"]["runtime_challenge"] == {
        "parity": "native",
        "origin": "gdaemon",
    }
    families = manifest["families"]
    assert families
    for name, family in families.items():
        assert family["parity"] in http_corpus.PARITY_VALUES, name
        assert family["origin"] in http_corpus.ORIGIN_VALUES, name
    assert manifest["cases"]
    for file_name in manifest["cases"]:
        case_path = http_corpus.CORPUS_DIR / file_name
        assert case_path.is_file(), file_name
        case = json.loads(case_path.read_text())
        assert case["family"] in families, file_name
        assert case["backend"] in http_corpus.BACKEND_VALUES, file_name
        assert case["credential"] in http_corpus.CREDENTIALS, file_name
        assert case["name"] == case_path.stem, file_name


def _synthetic_grant_body() -> dict[str, Any]:
    return {
        "grant": {
            "version": 2,
            "config_revision": 7,
            "deployment": {"token": SYNTHETIC_DEPLOYMENT_TOKEN, "fencing_epoch": 3},
            "capabilities": {
                "postgres": {
                    "mode": "direct",
                    "dsn": "postgresql://role:secret@127.0.0.1:5432/gobby",
                    "role_name": "gobby_grant_role",
                    "credential_generation": 1,
                    "valid_until": 1700000000,
                },
                "falkordb": {"mode": "direct", "host": "127.0.0.1", "port": 6379, "password": "fk"},
                "qdrant": {"mode": "direct", "url": "http://127.0.0.1:6333", "api_key": "qd"},
            },
            "issued_at": 1699990000,
            "expires_at": 1700000000,
            "payload_checksum": "sha256:abc",
            "signature": "ed25519:def",
        },
        "deployment_token": SYNTHETIC_DEPLOYMENT_TOKEN,
        "fencing_epoch": 3,
    }


@pytest.mark.unit
def test_secret_redaction_is_unconditional(tmp_path: Path) -> None:
    body = _synthetic_grant_body()
    secrets = (
        "postgresql://role:secret@127.0.0.1:5432/gobby",
        '"fk"',
        '"qd"',
        SYNTHETIC_DEPLOYMENT_TOKEN,
        "sha256:abc",
        "ed25519:def",
    )
    case = _synthetic_case("runtime_handshake", "runtime_handshake")
    case["response"] = {"status": 200, "headers": {}, "body": body}
    recordings = []
    for attempt in range(2):
        out = tmp_path / f"recording-{attempt}.json"
        out.write_bytes(http_corpus.case_bytes(http_corpus.normalize_case(case)))
        recordings.append(out.read_text())
    assert recordings[0] == recordings[1]
    for secret in secrets:
        assert secret not in recordings[0], secret

    redacted = json.loads(recordings[0])["response"]["body"]
    grant = redacted["grant"]
    assert redacted["deployment_token"] == http_corpus.SECRET
    assert redacted["fencing_epoch"] == 3
    assert grant["deployment"] == {"token": http_corpus.SECRET, "fencing_epoch": 3}
    assert grant["capabilities"]["postgres"] == {
        "mode": "direct",
        "dsn": http_corpus.SECRET,
        "role_name": "gobby_grant_role",
        "credential_generation": 1,
        "valid_until": 1700000000,
    }
    assert grant["capabilities"]["falkordb"]["password"] == http_corpus.SECRET
    assert grant["capabilities"]["falkordb"]["port"] == 6379
    assert grant["capabilities"]["qdrant"]["api_key"] == http_corpus.SECRET
    assert grant["payload_checksum"] == http_corpus.SECRET
    assert grant["signature"] == http_corpus.SECRET
    assert grant["config_revision"] == 7


@pytest.mark.e2e
def test_recorder_is_deterministic(boundary: BoundaryHarness, tmp_path: Path) -> None:
    credentials = _credentials(boundary)
    with _client(boundary) as client:
        first = http_corpus.record_cases(client, PYTHON_CASES, tmp_path / "a", **credentials)
        second = http_corpus.record_cases(client, PYTHON_CASES, tmp_path / "b", **credentials)
    forbidden = (
        daemon_token(boundary.home),
        encode_grant_header(boundary.grant),
        boundary.grant.deployment.token,
    )
    for left, right in zip(first, second, strict=True):
        assert left.read_bytes() == right.read_bytes(), left.name
        text = left.read_text()
        for value in forbidden:
            assert value not in text, left.name


@pytest.mark.e2e
@pytest.mark.parametrize("case", PYTHON_CASES, ids=[case["name"] for case in PYTHON_CASES])
def test_case_replays_equal(boundary: BoundaryHarness, case: dict[str, Any]) -> None:
    with _client(boundary) as client:
        expected, actual = http_corpus.replay_case(client, case, **_credentials(boundary))
    assert actual == expected


@pytest.mark.e2e
@pytest.mark.skipif(os.environ.get(RECORD_ENV) != "1", reason=f"set {RECORD_ENV}=1 to re-record")
def test_record_http_contracts(boundary: BoundaryHarness) -> None:
    with _client(boundary) as client:
        written = http_corpus.record_cases(
            client, PYTHON_CASES, http_corpus.CORPUS_DIR, **_credentials(boundary)
        )
    assert len(written) == len(PYTHON_CASES)
