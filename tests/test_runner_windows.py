"""Windows retains the pre-native PID and database lease primitives."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gobby.config.bootstrap import BootstrapConfig
from gobby.daemon_lease import ActiveDaemonLease
from gobby.runner_pid_file import FailOpenPidOwnership
from gobby.runner_windows import acquire_windows_lease, claim_windows_pid

pytestmark = pytest.mark.unit


def test_service_launch_consumes_existing_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_SERVICE_LAUNCH", "1")
    monkeypatch.setenv("GOBBY_SERVICE_NONCE", "owned-nonce")
    ownership = FailOpenPidOwnership(error="isolated fixture")
    pid_file = tmp_path / "gobby.pid"
    with patch(
        "gobby.runner_windows.convert_or_acquire_service_claim", return_value=ownership
    ) as claim:
        assert claim_windows_pid(pid_file) is ownership
    claim.assert_called_once_with(pid_file)
    import os

    assert "GOBBY_SERVICE_LAUNCH" not in os.environ
    assert "GOBBY_SERVICE_NONCE" not in os.environ


@pytest.mark.parametrize("acquired", [False, True])
async def test_windows_uses_existing_lease_and_standby_control(acquired: bool) -> None:
    bootstrap = BootstrapConfig(database_url="postgresql://isolated/test")
    lease = ActiveDaemonLease(
        "postgresql://isolated/test", machine_id="test-machine", deployment_token="test-deployment"
    )
    standby = AsyncMock(return_value=True)
    with (
        patch.object(lease, "try_acquire", return_value=acquired),
        patch("gobby.runner_windows.serve_standby_until_promotion", standby),
    ):
        assert await acquire_windows_lease(lease, bootstrap)
    if acquired:
        standby.assert_not_awaited()
    else:
        standby.assert_awaited_once()
        assert standby.await_args is not None
        assert standby.await_args.kwargs["port"] == bootstrap.daemon_port
    lease.release()
