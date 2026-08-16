"""Runner construction is fenced behind the active-daemon lease."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from gobby import runner as runner_module
from gobby.config.bootstrap import BootstrapConfig
from gobby.runner_pid_file import PidOwnershipResolution


@dataclass
class FakeOwnership:
    released: int = 0

    def release(self) -> None:
        self.released += 1


class FakeLease:
    def __init__(self, acquired: bool, events: list[str]) -> None:
        self.acquired = acquired
        self.events = events

    def try_acquire(self) -> bool:
        self.events.append("acquire")
        return self.acquired

    def heartbeat(self) -> None:
        self.events.append("heartbeat")

    def release(self) -> None:
        self.events.append("release")


@pytest.mark.asyncio
async def test_standby_never_constructs_full_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    lease = FakeLease(False, events)
    ownership = FakeOwnership()
    bootstrap = BootstrapConfig(
        database_url="postgresql://test.invalid/gobby_test",
        daemon_port=60991,
    )

    monkeypatch.setattr("gobby.config.bootstrap.load_bootstrap", lambda *_a, **_kw: bootstrap)
    monkeypatch.setattr("gobby.utils.machine_id.require_machine_id", lambda: "machine-a")
    monkeypatch.setattr("gobby.utils.local_token.read_local_api_token", lambda: "token")
    monkeypatch.setattr(
        "gobby.storage.schema_contract.verify_schema", lambda _url: events.append("verify")
    )
    monkeypatch.setattr("gobby.daemon_lease.ActiveDaemonLease", lambda *_a, **_kw: lease)

    async def serve(*_args: object, **_kwargs: object) -> bool:
        events.append("standby")
        return False

    monkeypatch.setattr("gobby.daemon_lease_control.serve_standby_until_promotion", serve)

    class ForbiddenRunner:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("standby constructed GobbyRunner")

    monkeypatch.setattr(runner_module, "GobbyRunner", ForbiddenRunner)

    await runner_module.run_gobby(
        config_path=Path("/tmp/bootstrap.yaml"),
        ownership_resolution=cast(PidOwnershipResolution, ownership),
    )

    assert events == ["verify", "acquire", "standby", "release"]
    assert ownership.released == 1


@pytest.mark.asyncio
async def test_active_constructs_runner_only_after_verify_and_acquire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    lease = FakeLease(True, events)
    ownership = FakeOwnership()
    bootstrap = BootstrapConfig(database_url="postgresql://test.invalid/gobby_test")

    monkeypatch.setattr("gobby.config.bootstrap.load_bootstrap", lambda *_a, **_kw: bootstrap)
    monkeypatch.setattr("gobby.utils.machine_id.require_machine_id", lambda: "machine-a")
    monkeypatch.setattr("gobby.utils.local_token.read_local_api_token", lambda: "token")
    monkeypatch.setattr(
        "gobby.storage.schema_contract.verify_schema", lambda _url: events.append("verify")
    )
    monkeypatch.setattr("gobby.daemon_lease.ActiveDaemonLease", lambda *_a, **_kw: lease)

    class FakeRunner:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            events.append("construct")
            self.http_server = type("HTTP", (), {"effect_fence": None})()

        @classmethod
        async def create(cls, *_args: object, **_kwargs: object) -> FakeRunner:
            return cls()

        async def run(self, *, ownership_resolution: object) -> None:
            events.append("run")

        def request_shutdown(self) -> None:
            events.append("shutdown")

    monkeypatch.setattr(runner_module, "GobbyRunner", FakeRunner)

    await runner_module.run_gobby(ownership_resolution=cast(PidOwnershipResolution, ownership))

    assert events[:4] == ["verify", "acquire", "construct", "run"]
    assert events[-1] == "release"
    assert ownership.released == 1
