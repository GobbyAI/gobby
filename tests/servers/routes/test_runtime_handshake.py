"""Handshake endpoint, challenge proof, and grant presentation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.runtime_grants.handshake import (
    HandshakeRejection,
    HandshakeService,
    challenge_proof,
    decode_grant_header,
    encode_grant_header,
)
from gobby.runtime_grants.launch import materialize_managed_launch
from gobby.runtime_grants.schema import GrantBundle, PostgresDirect
from gobby.runtime_grants.service import (
    DeploymentGrantContext,
    GrantService,
    StaleEpochGrant,
)
from gobby.servers.auth_service import _AGENT_CAPABILITY_MATRIX, AuthService
from gobby.utils.local_token import (
    issue_agent_api_token,
    verify_agent_api_token,
)
from tests.runtime_grants.support import (
    DEPLOYMENT_TOKEN,
    FENCING_EPOCH,
    GOLDEN_SECRET,
    StaticRuntime,
    config_snapshot,
    daemon_config,
)
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

OPERATOR_TOKEN = "handshake-operator-token"
LOCAL_MACHINE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROJECT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SESSION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
AGENT_RUN_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


@pytest.fixture(autouse=True)
def _machine_id() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _grant_service(*, epoch: int = FENCING_EPOCH) -> GrantService:
    snapshot = config_snapshot(daemon_config(), revision=3)
    return GrantService(
        runtime=StaticRuntime(snapshot),
        context=DeploymentGrantContext(
            token=DEPLOYMENT_TOKEN,
            fencing_epoch=epoch,
            signing_secret=GOLDEN_SECRET,
        ),
        clock=lambda: 1_700_000_000,
    )


def _postgres() -> PostgresDirect:
    return PostgresDirect(
        dsn="postgresql://gobby_ix_test:secret@127.0.0.1:60892/gobby_test",
        role_name="gobby_ix_test",
        credential_generation=1,
        valid_until=1_700_003_600,
    )


def _handshake(
    service: GrantService | None = None,
    *,
    admitted_maintenance_targets: Callable[[str], bool] | None = None,
) -> HandshakeService:
    grants = service or _grant_service()

    def issue_postgres(_principal: Any) -> PostgresDirect:
        return _postgres()

    return HandshakeService(
        grants=grants,
        local_machine_id=LOCAL_MACHINE_ID,
        operator_token=OPERATOR_TOKEN,
        issue_postgres=issue_postgres,
        admitted_projects=frozenset({PROJECT_ID}),
        admitted_maintenance_targets=admitted_maintenance_targets,
        clock=lambda: 1_700_000_000,
    )


def test_route_registered_in_app() -> None:
    server = create_http_server(config=DaemonConfig())
    paths = {getattr(route, "path", "") for route in server.app.routes}
    methods_by_path: dict[str, set[str]] = {}
    for route in server.app.routes:
        path = getattr(route, "path", "")
        methods_by_path.setdefault(path, set()).update(getattr(route, "methods", set()) or set())

    assert "/api/runtime/handshake" in paths
    assert "POST" in methods_by_path["/api/runtime/handshake"]
    assert "/api/runtime/handshake/challenge" in paths
    assert "POST" in methods_by_path["/api/runtime/handshake/challenge"]
    assert any(
        entry.method == "POST" and entry.route == "/api/runtime/handshake"
        for entry in _AGENT_CAPABILITY_MATRIX
    )


def test_challenge_proof_before_bearer(tmp_path: Path) -> None:
    nonce = os.urandom(16)
    interactive = challenge_proof(
        nonce,
        kind="interactive",
        operator_token=OPERATOR_TOKEN,
    )
    expected = hmac.new(OPERATOR_TOKEN.encode(), nonce, hashlib.sha256).hexdigest()
    assert interactive == expected

    token = issue_agent_api_token(
        OPERATOR_TOKEN,
        agent_run_id=AGENT_RUN_ID,
        session_id=SESSION_ID,
        project_id=PROJECT_ID,
        machine_id=LOCAL_MACHINE_ID,
        timeout_seconds=30,
    )
    claims = verify_agent_api_token(token, OPERATOR_TOKEN)
    assert claims is not None
    managed = challenge_proof(
        nonce,
        kind="managed",
        operator_token=OPERATOR_TOKEN,
        claims=claims,
    )
    signature = token.rsplit(".", maxsplit=1)[1]
    secret = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    assert managed == hmac.new(secret, nonce, hashlib.sha256).hexdigest()

    server = create_http_server(config=DaemonConfig(), authenticated_requests=False)
    token_file = tmp_path / "unused"
    server.auth_service = AuthService(lambda: server.services.database, token_file=token_file)
    with patch.object(server.auth_service, "local_token", return_value=OPERATOR_TOKEN):
        client = TestClient(server.app)
        rejected = client.post(
            "/api/runtime/handshake/challenge",
            json={"nonce": base64.urlsafe_b64encode(nonce).decode(), "kind": "interactive"},
            headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
        )
        assert rejected.status_code == 401
        assert rejected.json()["code"] == "credential_before_proof"
        allowed = client.post(
            "/api/runtime/handshake/challenge",
            json={"nonce": base64.urlsafe_b64encode(nonce).decode(), "kind": "interactive"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["proof"] == expected


def test_challenge_rejects_oversized_nonce() -> None:
    server = create_http_server(config=DaemonConfig(), authenticated_requests=False)
    client = TestClient(server.app)
    rejected = client.post(
        "/api/runtime/handshake/challenge",
        json={"nonce": "A" * 45, "kind": "interactive"},
    )
    assert rejected.status_code == 422


def test_machine_claim_binding() -> None:
    handshake = _handshake()
    missing = issue_agent_api_token(
        OPERATOR_TOKEN,
        agent_run_id=AGENT_RUN_ID,
        session_id=SESSION_ID,
        project_id=PROJECT_ID,
        machine_id=LOCAL_MACHINE_ID,
        timeout_seconds=30,
    )
    # Strip machine_id from a freshly signed token to prove verification fails.
    version, payload, _signature = missing.split(".", maxsplit=2)
    raw = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    raw.pop("machine_id", None)
    encoded = (
        base64.urlsafe_b64encode(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode()
    )
    signed = f"{version}.{encoded}"
    signature = hmac.new(OPERATOR_TOKEN.encode(), signed.encode(), hashlib.sha256).digest()
    unsigned_machine = f"{signed}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"
    assert verify_agent_api_token(unsigned_machine, OPERATOR_TOKEN) is None

    token = issue_agent_api_token(
        OPERATOR_TOKEN,
        agent_run_id=AGENT_RUN_ID,
        session_id=SESSION_ID,
        project_id=PROJECT_ID,
        machine_id=LOCAL_MACHINE_ID,
        timeout_seconds=30,
    )
    claims = verify_agent_api_token(token, OPERATOR_TOKEN)
    assert claims is not None
    assert claims.machine_id == LOCAL_MACHINE_ID
    with pytest.raises(HandshakeRejection, match="machine"):
        handshake.issue_for_agent(
            claims,
            machine_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
            project_id=PROJECT_ID,
        )


def test_bearer_claim_binding_matrix() -> None:
    handshake = _handshake()
    token = issue_agent_api_token(
        OPERATOR_TOKEN,
        agent_run_id=AGENT_RUN_ID,
        session_id=SESSION_ID,
        project_id=PROJECT_ID,
        machine_id=LOCAL_MACHINE_ID,
        timeout_seconds=30,
    )
    claims = verify_agent_api_token(token, OPERATOR_TOKEN)
    assert claims is not None

    with pytest.raises(HandshakeRejection) as mismatch:
        handshake.issue_for_agent(
            claims,
            machine_id=LOCAL_MACHINE_ID,
            project_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        )
    assert mismatch.value.code == "claims_mismatch"

    with pytest.raises(HandshakeRejection) as operator_machine:
        handshake.issue_for_operator(
            machine_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
            project_id=PROJECT_ID,
            session_id=SESSION_ID,
        )
    assert operator_machine.value.code == "claims_mismatch"

    with pytest.raises(HandshakeRejection) as unknown_project:
        handshake.issue_for_operator(
            machine_id=LOCAL_MACHINE_ID,
            project_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            session_id=SESSION_ID,
        )
    assert unknown_project.value.code == "claims_mismatch"

    with pytest.raises(HandshakeRejection) as managed_fail:
        HandshakeService(
            grants=_grant_service(),
            local_machine_id=LOCAL_MACHINE_ID,
            operator_token=OPERATOR_TOKEN,
            issue_postgres=lambda _principal: (_ for _ in ()).throw(
                HandshakeRejection("credential issuance failed", code="credential_issuance_failed")
            ),
            admitted_projects=frozenset({PROJECT_ID}),
            clock=lambda: 1_700_000_000,
        ).issue_for_agent(claims, machine_id=LOCAL_MACHINE_ID, project_id=PROJECT_ID)
    assert managed_fail.value.code == "credential_issuance_failed"


def test_maintenance_overlay_claim_binding() -> None:
    """issue_for_maintenance validates and carries an overlay claim (#20889)."""
    handshake = _handshake()
    overlay = "0d1a4ce8-6f21-5d59-8abc-9d2f5b2b8a7a"

    grant = handshake.issue_for_maintenance(
        machine_id=LOCAL_MACHINE_ID,
        project_id=PROJECT_ID,
        execution_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        code_overlay_project_id=overlay,
    )
    assert grant.principal.code_overlay_project_id == overlay

    bare = handshake.issue_for_maintenance(
        machine_id=LOCAL_MACHINE_ID,
        project_id=PROJECT_ID,
        execution_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    )
    assert bare.principal.code_overlay_project_id is None

    with pytest.raises(HandshakeRejection) as not_uuid:
        handshake.issue_for_maintenance(
            machine_id=LOCAL_MACHINE_ID,
            project_id=PROJECT_ID,
            execution_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            code_overlay_project_id="not-a-uuid",
        )
    assert not_uuid.value.code == "claims_mismatch"

    with pytest.raises(HandshakeRejection) as same_project:
        handshake.issue_for_maintenance(
            machine_id=LOCAL_MACHINE_ID,
            project_id=PROJECT_ID,
            execution_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            code_overlay_project_id=PROJECT_ID,
        )
    assert same_project.value.code == "claims_mismatch"


def test_maintenance_admits_indexed_project_targets_beyond_registry() -> None:
    """Maintenance grants may target indexed ids with no registry row (#21025)."""
    indexed_only = "0d1a4ce8-6f21-5d59-8abc-9d2f5b2b8a7c"
    handshake = _handshake(
        admitted_maintenance_targets=lambda project_id: project_id == indexed_only
    )

    grant = handshake.issue_for_maintenance(
        machine_id=LOCAL_MACHINE_ID,
        project_id=indexed_only,
        execution_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    )
    assert grant.principal.kind == "maintenance"
    assert grant.principal.project_id == indexed_only

    with pytest.raises(HandshakeRejection) as operator_rejected:
        handshake.issue_for_operator(
            machine_id=LOCAL_MACHINE_ID,
            project_id=indexed_only,
            session_id=SESSION_ID,
        )
    assert operator_rejected.value.code == "claims_mismatch"

    with pytest.raises(HandshakeRejection) as registry_only:
        _handshake().issue_for_maintenance(
            machine_id=LOCAL_MACHINE_ID,
            project_id=indexed_only,
            execution_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        )
    assert registry_only.value.code == "claims_mismatch"


def test_expiry_bounded_and_serialized() -> None:
    concurrent = 0
    peak = 0
    gate = threading.Lock()
    hold = threading.Event()

    def issue_postgres(_principal: Any) -> PostgresDirect:
        nonlocal concurrent, peak
        with gate:
            concurrent += 1
            peak = max(peak, concurrent)
            current = concurrent
        if current == 1:
            hold.wait(timeout=0.2)
        with gate:
            concurrent -= 1
        return PostgresDirect(
            dsn="postgresql://gobby_ix_test:secret@127.0.0.1:60892/gobby_test",
            role_name="gobby_ix_test",
            credential_generation=1,
            valid_until=1_700_000_400,
        )

    service = HandshakeService(
        grants=_grant_service(),
        local_machine_id=LOCAL_MACHINE_ID,
        operator_token=OPERATOR_TOKEN,
        issue_postgres=issue_postgres,
        admitted_projects=frozenset({PROJECT_ID}),
        clock=lambda: 1_700_000_000,
        principal_lock_key=lambda _kind, machine, project: (machine, project),
    )

    def run() -> GrantBundle:
        return service.issue_for_operator(
            machine_id=LOCAL_MACHINE_ID,
            project_id=PROJECT_ID,
            session_id=SESSION_ID,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run), pool.submit(run)]
        hold.set()
        first, second = futures[0].result(), futures[1].result()

    assert first.expires_at <= 1_700_000_400
    assert second.expires_at <= 1_700_000_400
    assert first.expires_at - first.issued_at <= 3600
    assert peak == 1


OVERLAY_PROJECT_ID = "eeeeeeee-eeee-5eee-8eee-eeeeeeeeeeee"


def test_operator_overlay_flows_into_principal() -> None:
    seen: list[Any] = []

    def issue_postgres(principal: Any) -> PostgresDirect:
        seen.append(principal)
        return _postgres()

    service = HandshakeService(
        grants=_grant_service(),
        local_machine_id=LOCAL_MACHINE_ID,
        operator_token=OPERATOR_TOKEN,
        issue_postgres=issue_postgres,
        admitted_projects=frozenset({PROJECT_ID}),
        clock=lambda: 1_700_000_000,
    )
    grant = service.issue_for_operator(
        machine_id=LOCAL_MACHINE_ID,
        project_id=PROJECT_ID,
        session_id=SESSION_ID,
        code_overlay_project_id=OVERLAY_PROJECT_ID,
    )
    assert grant.principal.code_overlay_project_id == OVERLAY_PROJECT_ID
    assert seen[0].code_overlay_project_id == OVERLAY_PROJECT_ID
    plain = service.issue_for_operator(
        machine_id=LOCAL_MACHINE_ID,
        project_id=PROJECT_ID,
        session_id=SESSION_ID,
    )
    assert plain.principal.code_overlay_project_id is None
    assert plain.payload_checksum != grant.payload_checksum

    for bad_overlay in ("not-a-uuid", PROJECT_ID):
        with pytest.raises(HandshakeRejection) as rejected:
            service.issue_for_operator(
                machine_id=LOCAL_MACHINE_ID,
                project_id=PROJECT_ID,
                session_id=SESSION_ID,
                code_overlay_project_id=bad_overlay,
            )
        assert rejected.value.code == "claims_mismatch"
    assert len(seen) == 2


def test_operator_issue_accepts_missing_session_id() -> None:
    """A sessionless interactive caller gets a grant with session_id None (#20899)."""
    seen: list[Any] = []

    def issue_postgres(principal: Any) -> PostgresDirect:
        seen.append(principal)
        return _postgres()

    service = HandshakeService(
        grants=_grant_service(),
        local_machine_id=LOCAL_MACHINE_ID,
        operator_token=OPERATOR_TOKEN,
        issue_postgres=issue_postgres,
        admitted_projects=frozenset({PROJECT_ID}),
        clock=lambda: 1_700_000_000,
    )
    grant = service.issue_for_operator(
        machine_id=LOCAL_MACHINE_ID,
        project_id=PROJECT_ID,
        session_id=None,
    )
    assert grant.principal.session_id is None
    assert seen[0].session_id is None


def test_handshake_endpoint_accepts_overlay(tmp_path: Path) -> None:
    grants = _grant_service()
    server = _config_server(grants, tmp_path / "token")
    client = TestClient(server.app)
    response = client.post(
        "/api/runtime/handshake",
        json={
            "machine_id": LOCAL_MACHINE_ID,
            "project_id": PROJECT_ID,
            "session_id": SESSION_ID,
            "code_overlay_project_id": OVERLAY_PROJECT_ID,
        },
    )
    assert response.status_code == 200
    assert response.json()["grant"]["principal"]["code_overlay_project_id"] == OVERLAY_PROJECT_ID


def test_epoch_bump_rejects_prior_grants(tmp_path: Path) -> None:
    grants = _grant_service(epoch=3)
    handshake = _handshake(grants)
    grant = handshake.issue_for_operator(
        machine_id=LOCAL_MACHINE_ID,
        project_id=PROJECT_ID,
        session_id=SESSION_ID,
    )
    assert grant.deployment.fencing_epoch == 3
    grants.context = DeploymentGrantContext(
        token=DEPLOYMENT_TOKEN,
        fencing_epoch=4,
        signing_secret=GOLDEN_SECRET,
    )
    with pytest.raises(StaleEpochGrant):
        grants.present(grant)

    server = _config_server(grants, tmp_path / "local_cli_token")
    client = TestClient(server.app)
    response = client.get(
        "/api/runtime/config",
        headers={
            "Authorization": f"Bearer {OPERATOR_TOKEN}",
            "X-Gobby-Runtime-Grant": encode_grant_header(grant),
            "X-Gobby-Machine-Id": grant.principal.machine_id,
            "X-Gobby-Caller-Project-Id": grant.principal.project_id,
            "X-Gobby-Project-Id": grant.principal.project_id,
            "X-Gobby-Session-Id": grant.principal.session_id or "",
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "stale_epoch"


def test_managed_refresh_envelope_token(tmp_path: Path) -> None:
    grants = _grant_service()
    handshake = _handshake(grants)
    claims = verify_agent_api_token(
        issue_agent_api_token(
            OPERATOR_TOKEN,
            agent_run_id=AGENT_RUN_ID,
            session_id=SESSION_ID,
            project_id=PROJECT_ID,
            machine_id=LOCAL_MACHINE_ID,
            timeout_seconds=90,
        ),
        OPERATOR_TOKEN,
    )
    assert claims is not None
    grant = handshake.issue_for_agent(
        claims,
        machine_id=LOCAL_MACHINE_ID,
        project_id=PROJECT_ID,
    )
    launch = materialize_managed_launch(
        grant,
        dest_dir=tmp_path,
        operator_token=OPERATOR_TOKEN,
        deadline_seconds=90,
    )
    assert launch.grant_path.is_file()
    assert launch.grant_path.stat().st_mode & 0o777 == 0o600
    assert launch.env["GOBBY_MANAGED_EXECUTION_BOOTSTRAP"] == str(launch.grant_path)
    envelope = launch.env["GOBBY_AGENT_API_TOKEN"]
    claims = verify_agent_api_token(envelope, OPERATOR_TOKEN)
    assert claims is not None
    assert claims.project_id == grant.principal.project_id
    assert claims.machine_id == grant.principal.machine_id
    assert claims.agent_run_id == AGENT_RUN_ID
    assert claims.exp >= grant.issued_at + 90

    refreshed = handshake.issue_for_agent(
        claims,
        machine_id=LOCAL_MACHINE_ID,
        project_id=PROJECT_ID,
    )
    assert refreshed.principal.kind == "agent_run"

    with pytest.raises(HandshakeRejection) as missing:
        handshake.authenticate_managed_refresh(None, grant.principal)
    assert missing.value.code == "managed_source"
    other = issue_agent_api_token(
        OPERATOR_TOKEN,
        agent_run_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        session_id=SESSION_ID,
        project_id=PROJECT_ID,
        machine_id=LOCAL_MACHINE_ID,
        timeout_seconds=30,
    )
    with pytest.raises(HandshakeRejection) as mismatched:
        handshake.authenticate_managed_refresh(other, grant.principal)
    assert mismatched.value.code == "managed_source"


def test_operator_and_agent_grants_are_v2() -> None:
    handshake = _handshake()
    operator = handshake.issue_for_operator(
        machine_id=LOCAL_MACHINE_ID,
        project_id=PROJECT_ID,
        session_id=SESSION_ID,
    )
    agent_token = issue_agent_api_token(
        OPERATOR_TOKEN,
        agent_run_id=AGENT_RUN_ID,
        session_id=SESSION_ID,
        project_id=PROJECT_ID,
        machine_id=LOCAL_MACHINE_ID,
        timeout_seconds=30,
    )
    agent_claims = verify_agent_api_token(agent_token, OPERATOR_TOKEN)
    assert agent_claims is not None
    agent = handshake.issue_for_agent(
        agent_claims,
        machine_id=LOCAL_MACHINE_ID,
        project_id=PROJECT_ID,
    )
    assert operator.version == 2
    assert agent.version == 2
    assert operator.principal.kind == "interactive"
    assert agent.principal.kind == "agent_run"
    assert decode_grant_header(encode_grant_header(operator)).signature == operator.signature


def _config_server(grants: GrantService, token_file: Path) -> Any:
    from gobby.servers.lease_fence import EffectFence

    server = create_http_server(config=DaemonConfig(), authenticated_requests=False)
    token_file.write_text(OPERATOR_TOKEN)
    server.auth_service = AuthService(lambda: server.services.database, token_file=token_file)
    server.auth_service.bind_runtime(
        grant_service=grants,
        lease_live=lambda: True,
        local_machine_id=LOCAL_MACHINE_ID,
        effect_fence=EffectFence(),
        clock=lambda: 1_700_000_000,
    )
    server.grant_service = grants
    server.handshake_service = _handshake(grants)

    def _accept(_request: object) -> bool:
        return True

    auth = cast(Any, server.auth_service)
    auth.is_request_authenticated = _accept
    auth._legacy_authenticated = _accept
    auth._credential_accepted = _accept
    return server


def test_maintenance_admits_soft_deleted_purge_targets_the_operator_path_refuses() -> None:
    """A soft-deleted project being purged is granted only as a maintenance target (#21458)."""
    soft_deleted = "5b2d9f0e-3c4a-4d6e-8f1a-2b3c4d5e6f70"
    handshake = _handshake(
        admitted_maintenance_targets=lambda project_id: project_id == soft_deleted
    )

    grant = handshake.issue_for_maintenance(
        machine_id=LOCAL_MACHINE_ID,
        project_id=soft_deleted,
        execution_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    )
    assert grant.principal.kind == "maintenance"
    assert grant.principal.project_id == soft_deleted

    with pytest.raises(HandshakeRejection) as operator_rejected:
        handshake.issue_for_operator(
            machine_id=LOCAL_MACHINE_ID,
            project_id=soft_deleted,
            session_id=SESSION_ID,
        )
    assert operator_rejected.value.code == "claims_mismatch"


def test_maintenance_target_admission_query_covers_indexed_and_soft_deleted_projects() -> None:
    """The daemon's admission query names both the code-index identity and soft-deleted rows."""
    from unittest.mock import MagicMock

    from gobby.runner_init.servers import maintenance_target_admitted

    database = MagicMock()
    database.fetchone.return_value = {"?column?": 1}
    assert maintenance_target_admitted(database, "p1") is True
    sql, params = database.fetchone.call_args.args
    assert "FROM code_indexed_projects WHERE id = %s" in sql
    assert "FROM projects WHERE id = %s AND deleted_at IS NOT NULL" in sql
    assert params == ("p1", "p1")

    database.fetchone.return_value = None
    assert maintenance_target_admitted(database, "p1") is False
