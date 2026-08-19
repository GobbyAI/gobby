"""Tests for managed credential leases around ToolChat requests."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from gobby.ai._managed_tool_chat_lease import build_managed_tool_chat_lease_factory
from gobby.ai._tool_chat_contracts import ToolChatRequest, ToolPolicy
from gobby.runtime_grants.schema import GrantBundle
from gobby.storage.managed_credentials import CredentialAuthorizationError
from gobby.utils.local_token import verify_agent_api_token

pytestmark = pytest.mark.unit

_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
_OPERATOR_TOKEN = "operator-token"
_DEPLOYMENT_TOKEN = "cafebabedeadbeef"
_SIGNING_SECRET = "sec-live"


def _write_bootstrap(tmp_path: Path, execution_id: object) -> Path:
    bootstrap_path = tmp_path / "managed" / "bootstrap.json"
    bootstrap_path.parent.mkdir(parents=True)
    bootstrap_path.write_text(
        json.dumps(
            {
                "database_url": "postgresql://tool:secret@127.0.0.1:5432/gobby",
                "credential_generation": 3,
                "managed_execution_id": str(execution_id),
                "role_name": "gobby_tool_chat",
            }
        ),
        encoding="utf-8",
    )
    return bootstrap_path


def _issued_credential(
    tmp_path: Path,
    *,
    execution_id: object,
    project_id: object,
) -> SimpleNamespace:
    issued_at = datetime(2026, 8, 19, tzinfo=UTC)
    return SimpleNamespace(
        project_id=project_id,
        project_path="/authoritative/repo",
        credential=SimpleNamespace(
            managed_execution_id=execution_id,
            role_name="gobby_tool_chat",
            credential_generation=3,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=300),
            bootstrap_path=_write_bootstrap(tmp_path, execution_id),
        ),
    )


def _patch_lease_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.ai._managed_tool_chat_lease.read_local_api_token",
        lambda: _OPERATOR_TOKEN,
    )
    monkeypatch.setattr(
        "gobby.ai._managed_tool_chat_lease.get_machine_id",
        lambda: _MACHINE_ID,
    )
    monkeypatch.setattr(
        "gobby.daemon_lease.current_lease",
        lambda: SimpleNamespace(
            deployment_token=_DEPLOYMENT_TOKEN,
            fencing_epoch=9,
            grant_signing_secret=_SIGNING_SECRET,
        ),
    )


@pytest.mark.asyncio
async def test_lease_replaces_client_path_and_revokes_after_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session_id = uuid4()
    execution_id = uuid4()
    project_id = uuid4()
    issued = _issued_credential(tmp_path, execution_id=execution_id, project_id=project_id)
    manager = MagicMock()
    manager.issue_tool_request.return_value = issued
    _patch_lease_dependencies(monkeypatch)
    factory = build_managed_tool_chat_lease_factory(manager)
    request = ToolChatRequest(
        prompt="Inspect auth.",
        tool_policy=ToolPolicy(cli="gcode", tools=("search",)),
        project_path="/client/spoof",
        session_id=session_id,
    )
    grant_path = issued.credential.bootstrap_path.parent / "grant.json"

    async with factory(request, 30.0) as scoped:
        assert scoped.project_path == "/authoritative/repo"
        assert scoped.managed_execution_id == execution_id
        assert scoped.credential_bootstrap_path == str(grant_path)
        subprocess_env = scoped.managed_subprocess_env
        assert subprocess_env["GOBBY_MANAGED_EXECUTION_BOOTSTRAP"] == str(grant_path)
        assert subprocess_env["GOBBY_MANAGED_EXECUTION_BOOTSTRAP"].endswith("grant.json")
        assert subprocess_env["GOBBY_MANAGED_EXECUTION_ID"] == str(execution_id)
        assert subprocess_env["GOBBY_SESSION_ID"] == str(session_id)
        assert subprocess_env["GOBBY_PROJECT_ID"] == str(project_id)
        grant = GrantBundle.model_validate_json(grant_path.read_bytes())
        assert grant.principal.kind == "tool_chat"
        assert grant.principal.execution_id == str(execution_id)
        assert grant.principal.session_id == str(session_id)
        assert grant.principal.project_id == str(project_id)
        assert grant.principal.machine_id == _MACHINE_ID
        assert grant.signature
        claims = verify_agent_api_token(
            subprocess_env["GOBBY_AGENT_API_TOKEN"],
            _OPERATOR_TOKEN,
        )
        assert claims is not None
        assert claims.managed_execution_id == str(execution_id)
        assert claims.agent_run_id is None

    manager.issue_tool_request.assert_called_once()
    manager.revoke.assert_called_once_with(
        execution_id,
        reason="tool-request-finally",
    )


@pytest.mark.asyncio
async def test_lease_revokes_credential_if_cancelled_during_issuance() -> None:
    session_id = uuid4()
    execution_id = uuid4()
    issue_started = threading.Event()
    allow_issue = threading.Event()
    issue_finished = threading.Event()
    manager = MagicMock()

    def issue_tool_request(**_kwargs: object) -> SimpleNamespace:
        issue_started.set()
        assert allow_issue.wait(timeout=5)
        issue_finished.set()
        return SimpleNamespace(
            project_path="/authoritative/repo",
            credential=SimpleNamespace(
                managed_execution_id=execution_id,
                bootstrap_path=Path("/private/runtime/managed/bootstrap.json"),
            ),
        )

    manager.issue_tool_request.side_effect = issue_tool_request
    factory = build_managed_tool_chat_lease_factory(manager)
    request = ToolChatRequest(
        prompt="Inspect auth.",
        tool_policy=ToolPolicy(cli="gcode", tools=("search",)),
        project_path="/client/spoof",
        session_id=session_id,
    )

    async def consume_lease() -> None:
        async with factory(request, 30.0):
            pytest.fail("cancelled request must not enter the lease body")

    task = asyncio.create_task(consume_lease())
    assert await asyncio.to_thread(issue_started.wait, 5)
    task.cancel()
    allow_issue.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(issue_finished.wait, 5)
    manager.revoke.assert_called_once_with(
        execution_id,
        reason="tool-request-finally",
    )


@pytest.mark.asyncio
async def test_lease_revokes_and_skips_grant_when_lease_context_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session_id = uuid4()
    execution_id = uuid4()
    issued = _issued_credential(tmp_path, execution_id=execution_id, project_id=uuid4())
    manager = MagicMock()
    manager.issue_tool_request.return_value = issued
    monkeypatch.setattr(
        "gobby.ai._managed_tool_chat_lease.read_local_api_token",
        lambda: _OPERATOR_TOKEN,
    )
    monkeypatch.setattr("gobby.daemon_lease.current_lease", lambda: None)
    factory = build_managed_tool_chat_lease_factory(manager)
    request = ToolChatRequest(
        prompt="Inspect auth.",
        tool_policy=ToolPolicy(cli="gcode", tools=("search",)),
        project_path="/client/spoof",
        session_id=session_id,
    )
    grant_path = issued.credential.bootstrap_path.parent / "grant.json"

    with pytest.raises(CredentialAuthorizationError, match="grant signing context"):
        async with factory(request, 30.0):
            pytest.fail("missing grant context must not enter the lease body")

    assert not grant_path.exists()
    manager.revoke.assert_called_once_with(
        execution_id,
        reason="tool-request-finally",
    )
