"""Maintenance launch issuance and revoke-on-cleanup."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from gobby.runtime_grants import GrantBundle
from gobby.runtime_grants.handshake import HandshakeService
from gobby.runtime_grants.launch import ManagedLaunch
from gobby.runtime_grants.maintenance import HandshakeMaintenanceLaunchFactory
from gobby.storage.managed_credentials import ManagedCredentialManager
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _maintenance_bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.utils import local_token

    bootstrap = tmp_path / "daemon-bootstrap.yaml"
    bootstrap.write_text("api_key: maintenance-test-key\n")
    monkeypatch.setattr(local_token, "_daemon_bootstrap", None)
    local_token.bind_daemon_bootstrap(bootstrap)


_GOLDEN = Path(__file__).resolve().parent / "golden" / "direct_datastores.json"


def test_launch_signs_with_current_bootstrap_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    from starlette.requests import Request

    from gobby.runtime_grants.handshake import HandshakeRejection
    from gobby.servers.auth_service import AuthService
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.utils import local_token

    bootstrap = tmp_path / "bootstrap.yaml"
    bootstrap.write_text("api_key: first-key\n")
    monkeypatch.setattr(local_token, "_daemon_bootstrap", None)
    local_token.bind_daemon_bootstrap(bootstrap)
    handshake = _Handshake()
    credentials = _RecordingCredentials()
    factory = HandshakeMaintenanceLaunchFactory(
        handshake=cast(HandshakeService, handshake),
        credentials=cast(ManagedCredentialManager, credentials),
        machine_id="machine-1",
    )
    database = MagicMock(spec=HubDatabase)
    database.fetchone.return_value = {"login_capable": True}
    monkeypatch.setattr("gobby.servers.auth_service.resolve_auth_schema", lambda _: "auth")
    service = AuthService(
        lambda: database, bootstrap_file=bootstrap, break_glass_file=tmp_path / "absent-break-glass"
    )
    tokens: list[str] = []
    for key in ("first-key", "second-key"):
        replacement = tmp_path / "replacement.yaml"
        replacement.write_text(f"api_key: {key}\n")
        replacement.replace(bootstrap)
        with factory.open("project-1", timeout_seconds=30) as launch:
            token = launch.env["GOBBY_AGENT_API_TOKEN"]
            tokens.append(token)
            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/providers/models",
                    "headers": [(b"authorization", f"Bearer {token}".encode())],
                    "query_string": b"",
                }
            )
            assert service.authenticate(request).allowed
    assert tokens[0] != tokens[1]
    assert local_token.verify_agent_api_token(tokens[0], service.managed_signing_key()) is None
    bootstrap.unlink()
    with pytest.raises(HandshakeRejection, match="signing_key_unavailable") as error:
        with factory.open("project-1", timeout_seconds=30):
            pytest.fail("missing key admitted a maintenance launch")
    assert error.value.code == "signing_key_unavailable"
    assert handshake.calls == 2


class _RecordingCredentials:
    def __init__(self, *, fail: bool = False) -> None:
        self.revoked: list[tuple[UUID, str]] = []
        self._fail = fail

    def revoke(self, execution_id: UUID, reason: str) -> None:
        self.revoked.append((execution_id, reason))
        if self._fail:
            raise RuntimeError("revoke failed")


class _Handshake:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0
        self.issued_kwargs: list[dict[str, Any]] = []

    def issue_for_maintenance(self, **kwargs: Any) -> GrantBundle:
        self.calls += 1
        self.issued_kwargs.append(kwargs)
        if self.error is not None:
            raise self.error
        grant = GrantBundle.model_validate_json(_GOLDEN.read_bytes())
        principal = grant.principal.model_copy(
            update={
                "kind": "maintenance",
                "session_id": None,
                "execution_id": kwargs["execution_id"],
                "machine_id": kwargs["machine_id"],
                "project_id": kwargs["project_id"],
            }
        )
        return grant.model_copy(update={"principal": principal})


def _factory(
    handshake: _Handshake,
    credentials: _RecordingCredentials,
) -> HandshakeMaintenanceLaunchFactory:
    return HandshakeMaintenanceLaunchFactory(
        handshake=cast(HandshakeService, handshake),
        credentials=cast(ManagedCredentialManager, credentials),
        machine_id="machine-1",
    )


def test_open_does_not_revoke_when_issue_fails() -> None:
    handshake = _Handshake(error=RuntimeError("issue failed"))
    credentials = _RecordingCredentials()
    factory = _factory(handshake, credentials)

    with (
        pytest.raises(RuntimeError, match="issue failed"),
        factory.open("project-1", timeout_seconds=1),
    ):
        raise AssertionError("unreachable")

    assert handshake.calls == 1
    assert credentials.revoked == []


def test_open_revokes_only_after_issue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    handshake = _Handshake()
    credentials = _RecordingCredentials()
    factory = _factory(handshake, credentials)
    launch = ManagedLaunch(grant_path=tmp_path / "grant.json", env={})

    def _return_launch(*_args: object, **_kwargs: object) -> ManagedLaunch:
        return launch

    monkeypatch.setattr(
        "gobby.runtime_grants.maintenance.materialize_managed_launch",
        _return_launch,
    )

    with factory.open("project-1", timeout_seconds=1) as opened:
        assert opened is launch

    assert len(credentials.revoked) == 1
    assert credentials.revoked[0][1] == "maintenance-complete"


def test_open_forwards_overlay_claim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    handshake = _Handshake()
    credentials = _RecordingCredentials()
    factory = _factory(handshake, credentials)
    launch = ManagedLaunch(grant_path=tmp_path / "grant.json", env={})

    monkeypatch.setattr(
        "gobby.runtime_grants.maintenance.materialize_managed_launch",
        lambda *_args, **_kwargs: launch,
    )

    overlay = "0d1a4ce8-6f21-5d59-8abc-9d2f5b2b8a7a"
    with factory.open("project-1", timeout_seconds=1, code_overlay_project_id=overlay):
        pass
    with factory.open("project-1", timeout_seconds=1):
        pass

    claims = [kwargs["code_overlay_project_id"] for kwargs in handshake.issued_kwargs]
    assert claims == [overlay, None]


def test_open_preserves_body_error_when_revoke_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    handshake = _Handshake()
    credentials = _RecordingCredentials(fail=True)
    factory = _factory(handshake, credentials)
    launch = ManagedLaunch(grant_path=tmp_path / "grant.json", env={})

    def _return_launch(*_args: object, **_kwargs: object) -> ManagedLaunch:
        return launch

    monkeypatch.setattr(
        "gobby.runtime_grants.maintenance.materialize_managed_launch",
        _return_launch,
    )

    with (
        pytest.raises(ValueError, match="body failed"),
        factory.open("project-1", timeout_seconds=1),
    ):
        raise ValueError("body failed")

    assert len(credentials.revoked) == 1


@pytest.mark.asyncio
async def test_open_async_yields_launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    handshake = _Handshake()
    credentials = _RecordingCredentials()
    factory = _factory(handshake, credentials)
    launch = ManagedLaunch(grant_path=tmp_path / "grant.json", env={})

    def _return_launch(*_args: object, **_kwargs: object) -> ManagedLaunch:
        return launch

    monkeypatch.setattr(
        "gobby.runtime_grants.maintenance.materialize_managed_launch",
        _return_launch,
    )

    async with factory.open_async("project-1", timeout_seconds=1) as opened:
        assert opened is launch

    assert len(credentials.revoked) == 1


@pytest.mark.asyncio
async def test_open_async_double_cancel_during_entry_still_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import asyncio

    handshake = _Handshake()
    credentials = _RecordingCredentials()
    factory = _factory(handshake, credentials)
    launch = ManagedLaunch(grant_path=tmp_path / "grant.json", env={})
    monkeypatch.setattr(
        "gobby.runtime_grants.maintenance.materialize_managed_launch",
        lambda *_args, **_kwargs: launch,
    )
    real_to_thread = asyncio.to_thread
    entered = asyncio.Event()

    async def slow_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(func, "__name__", "") == "__enter__":
            entered.set()
            await drain_asyncio_tasks(cycles=5)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr("gobby.runtime_grants.maintenance.asyncio.to_thread", slow_to_thread)

    async def _run() -> None:
        async with factory.open_async("project-1", timeout_seconds=1):
            pass

    task = asyncio.create_task(_run())
    await drain_asyncio_tasks(cycles=8)
    assert entered.is_set() or task.done()
    task.cancel()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(credentials.revoked) == 1
