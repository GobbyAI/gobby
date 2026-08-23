"""Handshake Postgres issuance fail-closed helpers."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from gobby.runner_init.servers import issue_grant_postgres
from gobby.runtime_grants.handshake import HandshakeRejection
from gobby.runtime_grants.schema import GrantPrincipal

pytestmark = pytest.mark.unit


def _interactive(*, session_id: str | None) -> GrantPrincipal:
    return GrantPrincipal(
        kind="interactive",
        machine_id="machine-1",
        project_id=str(uuid4()),
        execution_id=None,
        session_id=session_id,
    )


def test_interactive_missing_session_id_is_claims_mismatch() -> None:
    credentials = MagicMock()
    with pytest.raises(HandshakeRejection) as rejected:
        issue_grant_postgres(
            _interactive(session_id=None),
            credentials=credentials,
            deployment_token="token",
            secrets=object(),
            managed_bootstrap_dsn=lambda _path: "x",
        )
    assert rejected.value.code == "claims_mismatch"
    credentials.issue_interactive.assert_not_called()


def test_issuance_errors_are_generic() -> None:
    credentials = MagicMock()
    credentials.issue_interactive.side_effect = RuntimeError("dsn=postgres://secret")
    with pytest.raises(HandshakeRejection) as rejected:
        issue_grant_postgres(
            _interactive(session_id=str(uuid4())),
            credentials=credentials,
            deployment_token="token",
            secrets=object(),
            managed_bootstrap_dsn=lambda _path: "x",
        )
    assert rejected.value.code == "credential_issuance_failed"
    assert rejected.value.message == "credential issuance failed"
    assert "secret" not in rejected.value.message
    assert "postgres://" not in rejected.value.message


def test_interactive_overlay_is_passed_to_issuer() -> None:
    credentials = MagicMock()
    issued = credentials.issue_interactive.return_value
    issued.dsn = "postgresql://scoped"
    issued.role_name = "gobby_ix_test_2"
    issued.credential_generation = 2
    issued.expires_at.timestamp.return_value = 1_700_003_600
    overlay = uuid4()
    principal = _interactive(session_id=str(uuid4())).model_copy(
        update={"code_overlay_project_id": str(overlay)}
    )
    issue_grant_postgres(
        principal,
        credentials=credentials,
        deployment_token="token",
        secrets=object(),
        managed_bootstrap_dsn=lambda _path: "x",
    )
    assert credentials.issue_interactive.call_args.kwargs["code_overlay_project_id"] == overlay


def test_unregistered_overlay_is_claims_mismatch() -> None:
    from gobby.storage.managed_credentials import CredentialAuthorizationError

    credentials = MagicMock()
    credentials.issue_interactive.side_effect = CredentialAuthorizationError(
        "interactive overlay is not a registered isolation workspace of the project"
    )
    principal = _interactive(session_id=str(uuid4())).model_copy(
        update={"code_overlay_project_id": str(uuid4())}
    )
    with pytest.raises(HandshakeRejection) as rejected:
        issue_grant_postgres(
            principal,
            credentials=credentials,
            deployment_token="token",
            secrets=object(),
            managed_bootstrap_dsn=lambda _path: "x",
        )
    assert rejected.value.code == "claims_mismatch"
    assert "registered isolation workspace" in rejected.value.message
