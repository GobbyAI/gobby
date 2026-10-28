"""An expired seat capability renews its grant only while the seat's run is live."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.runner_init.servers import issue_grant_postgres
from gobby.runtime_grants.handshake import HandshakeService
from gobby.runtime_grants.service import DeploymentGrantContext, GrantService
from gobby.servers.auth_service import AuthService
from gobby.servers.routes.runtime_handshake import create_runtime_handshake_router
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.utils.local_token import derive_managed_signing_key, issue_agent_api_token
from tests.runtime_grants.support import (
    DEPLOYMENT_TOKEN,
    FENCING_EPOCH,
    GOLDEN_SECRET,
    StaticRuntime,
    config_snapshot,
    daemon_config,
)
from tests.storage.test_managed_credential_recovery import _dsn
from tests.storage.test_managed_credentials import _manager, _secret_store
from tests.storage.test_postgres_agent_authorization import AuthorizationFixture
from tests.storage.test_postgres_agent_authorization import (
    fresh_authorization_fixture as authorization_fixture,
)

pytestmark = pytest.mark.integration
__all__ = ["authorization_fixture"]

OPERATOR_TOKEN = "renewal-operator-token"


def _expired_token(
    fixture: AuthorizationFixture,
    *,
    signing_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """Mint a seat capability whose expiry passed long ago, as after a long launch."""
    launched_at = time.time() - 7200
    with monkeypatch.context() as clock:
        clock.setattr("gobby.utils.local_token.time.time", lambda: launched_at)
        return issue_agent_api_token(
            signing_key,
            agent_run_id=str(fixture.agent_run_id),
            session_id=str(fixture.session_id),
            project_id=str(fixture.project_id),
            machine_id=str(fixture.machine_id),
            timeout_seconds=60,
        )


def _client(
    fixture: AuthorizationFixture,
    *,
    manager: Any,
    store: Any,
    tmp_path: Path,
) -> TestClient:
    """Real auth and handshake route over the real Postgres issuance seam."""
    bootstrap = tmp_path / "bootstrap.yaml"
    bootstrap.write_text(json.dumps({"api_key": OPERATOR_TOKEN}))
    grants = GrantService(
        runtime=StaticRuntime(config_snapshot(daemon_config(), revision=3)),
        context=DeploymentGrantContext(
            token=DEPLOYMENT_TOKEN,
            fencing_epoch=FENCING_EPOCH,
            signing_secret=GOLDEN_SECRET,
        ),
        clock=lambda: int(time.time()),
    )
    handshake = HandshakeService(
        grants=grants,
        local_machine_id=str(fixture.machine_id),
        issue_postgres=lambda principal: issue_grant_postgres(
            principal,
            credentials=manager,
            deployment_token=DEPLOYMENT_TOKEN,
            secrets=store,
            managed_bootstrap_dsn=_dsn,
        ),
        admitted_projects=frozenset({str(fixture.project_id)}),
        clock=lambda: int(time.time()),
    )
    auth = AuthService(lambda: store.db, bootstrap_file=bootstrap)
    auth.bind_runtime(
        grant_service=grants,
        local_machine_id=str(fixture.machine_id),
        clock=lambda: int(time.time()),
    )
    server = SimpleNamespace(
        auth_service=auth,
        handshake_service=handshake,
        services=SimpleNamespace(database=store.db),
    )
    app = FastAPI()
    app.include_router(create_runtime_handshake_router(server))
    return TestClient(app)


def _headers(fixture: AuthorizationFixture, token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Gobby-Project-Id": str(fixture.project_id),
        "X-Gobby-Session-Id": str(fixture.session_id),
        "X-Gobby-Agent-Run-Id": str(fixture.agent_run_id),
    }


def _body(fixture: AuthorizationFixture) -> dict[str, str]:
    return {
        "machine_id": str(fixture.machine_id),
        "project_id": str(fixture.project_id),
        "session_id": str(fixture.session_id),
    }


def test_expired_seat_capability_renews_its_grant(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    store = _secret_store(fixture)
    try:
        first = manager.issue(
            managed_execution_id=fixture.agent_run_id,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=59),
        )
        token = _expired_token(
            fixture,
            signing_key=derive_managed_signing_key(OPERATOR_TOKEN),
            monkeypatch=monkeypatch,
        )
        client = _client(fixture, manager=manager, store=store, tmp_path=tmp_path)

        response = client.post(
            "/api/runtime/handshake",
            json=_body(fixture),
            headers=_headers(fixture, token),
        )

        assert response.status_code == 200, response.text
        grant = response.json()["grant"]
        assert grant["principal"]["kind"] == "agent_run"
        # The expired capability must not bound the renewed grant to its own lifetime.
        assert grant["expires_at"] - grant["issued_at"] >= 60
        assert manager.get_live_binding_generation(fixture.agent_run_id) == (
            first.credential_generation + 1
        )
    finally:
        manager.revoke(fixture.agent_run_id, reason="test-cleanup")
        manager.close()
        store.db.close()


@pytest.mark.parametrize("refusal", ["ended_run", "forged_signature", "other_session"])
def test_expired_capability_renews_only_for_live_matching_seat(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    store = _secret_store(fixture)
    try:
        first = manager.issue(
            managed_execution_id=fixture.agent_run_id,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=59),
        )
        signing_key = derive_managed_signing_key(
            "attacker-operator-token" if refusal == "forged_signature" else OPERATOR_TOKEN
        )
        token = _expired_token(fixture, signing_key=signing_key, monkeypatch=monkeypatch)
        headers = _headers(fixture, token)
        if refusal == "other_session":
            headers["X-Gobby-Session-Id"] = str(uuid.uuid4())
        if refusal == "ended_run":
            with psycopg.connect(fixture.database_url, autocommit=True) as admin:
                admin.execute(
                    "UPDATE agent_runs SET status = 'completed' WHERE id = %s",
                    (fixture.agent_run_id,),
                )
        client = _client(fixture, manager=manager, store=store, tmp_path=tmp_path)

        response = client.post("/api/runtime/handshake", json=_body(fixture), headers=headers)

        assert response.status_code == 401, response.text
        assert manager.get_live_binding_generation(fixture.agent_run_id) == (
            first.credential_generation
        )
    finally:
        manager.revoke(fixture.agent_run_id, reason="test-cleanup")
        manager.close()
        store.db.close()


def test_persistent_renewal_failure_reports_capability_expiry_to_parent(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    store = _secret_store(fixture)
    monkeypatch.setattr("time.sleep", lambda _delay: None)
    try:
        manager.issue(
            managed_execution_id=fixture.agent_run_id,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=59),
        )
        token = _expired_token(
            fixture,
            signing_key=derive_managed_signing_key(OPERATOR_TOKEN),
            monkeypatch=monkeypatch,
        )
        client = _client(fixture, manager=manager, store=store, tmp_path=tmp_path)

        # The pool stays saturated until the seat's capability has expired.
        with patch.object(
            manager,
            "_rotate_if_generation",
            side_effect=TimeoutError("test: saturated credential pool"),
        ):
            first = client.post(
                "/api/runtime/handshake", json=_body(fixture), headers=_headers(fixture, token)
            )
            retry = client.post(
                "/api/runtime/handshake", json=_body(fixture), headers=_headers(fixture, token)
            )

        assert first.json()["code"] == "credential_issuance_failed"
        assert retry.json()["code"] == "credential_issuance_failed"
        run = LocalAgentRunManager(store.db).get(str(fixture.agent_run_id))
        assert run is not None
        inbox = InterSessionMessageManager(store.db).get_messages(run.parent_session_id)
        blockers = [
            message
            for message in inbox
            if message.message_type == "agent_recovery"
            and json.loads(message.metadata_json or "{}").get("event") == "capability_expired"
        ]
        # Re-handshakes for the same expiry report one typed blocker, not one per attempt.
        assert len(blockers) == 1
        assert json.loads(blockers[0].metadata_json or "{}")["run_id"] == str(fixture.agent_run_id)
        assert "expired" in blockers[0].content
    finally:
        manager.revoke(fixture.agent_run_id, reason="test-cleanup")
        manager.close()
        store.db.close()
