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

pytestmark = pytest.mark.unit

_GOLDEN = Path(__file__).resolve().parent / "golden" / "direct_datastores.json"


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

    def issue_for_maintenance(self, **_kwargs: Any) -> GrantBundle:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return GrantBundle.model_validate_json(_GOLDEN.read_bytes())


def _factory(
    handshake: _Handshake,
    credentials: _RecordingCredentials,
) -> HandshakeMaintenanceLaunchFactory:
    return HandshakeMaintenanceLaunchFactory(
        handshake=cast(HandshakeService, handshake),
        credentials=cast(ManagedCredentialManager, credentials),
        operator_token="operator",
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
