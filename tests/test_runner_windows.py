"""Remote-mode Windows runners retain PID reservation support."""

from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.runner_pid_file import FailOpenPidOwnership
from gobby.runner_windows import claim_windows_pid

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
