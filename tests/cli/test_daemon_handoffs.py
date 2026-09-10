"""CLI admission refusal, bounded waiting, and lock lifetime."""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from gobby.cli._daemon_handoffs import protect_pending_handoffs
from gobby.cli.daemon import _do_stop
from gobby.cli.runtime import CliRuntime
from gobby.sessions.handoff_shutdown import HandoffShutdownBlocked
from gobby.storage.hub.protocol import HubDatabase


@pytest.mark.parametrize("wait", [False, True])
def test_cli_holds_admission_fence_through_stop_and_releases_on_error(wait: bool) -> None:
    events: list[str] = []

    @contextmanager
    def guard(db: HubDatabase, machine_id: str) -> Iterator[None]:
        events.append("admission closed")
        try:
            yield
        finally:
            events.append("admission reopened")

    runtime = MagicMock(spec=CliRuntime)
    with (
        patch("gobby.cli._daemon_handoffs.guard_handoff_shutdown", side_effect=guard),
        pytest.raises(RuntimeError, match="signal failed"),
        protect_pending_handoffs(runtime, wait=wait, report=events.append),
    ):
        events.append("signal sent")
        raise RuntimeError("signal failed")
    assert events == ["admission closed", "signal sent", "admission reopened"]
    runtime.require_database.assert_called_once_with(apply_migrations=False)


def test_wait_rechecks_after_handoff_acknowledgment() -> None:
    pending = True
    messages: list[str] = []

    @contextmanager
    def guard(db: HubDatabase, machine_id: str) -> Iterator[None]:
        if pending:
            raise HandoffShutdownBlocked("pending #12332")
        yield

    def acknowledge(_seconds: float) -> None:
        nonlocal pending
        pending = False

    with (
        patch("gobby.cli._daemon_handoffs.guard_handoff_shutdown", side_effect=guard),
        patch("gobby.cli._daemon_handoffs.time.sleep", side_effect=acknowledge),
        protect_pending_handoffs(MagicMock(spec=CliRuntime), wait=True, report=messages.append),
    ):
        assert pending is False
    assert messages == ["Waiting for handoff completion: pending #12332"]


def test_wait_timeout_refuses_shutdown() -> None:
    messages: list[str] = []
    with (
        patch(
            "gobby.cli._daemon_handoffs.guard_handoff_shutdown",
            side_effect=HandoffShutdownBlocked("unresolved #12332"),
        ) as check,
        patch("gobby.cli._daemon_handoffs.time.monotonic", side_effect=[0, 601]),
        patch("gobby.cli._daemon_handoffs.time.sleep") as sleep,
        pytest.raises(HandoffShutdownBlocked, match="unresolved"),
        protect_pending_handoffs(MagicMock(spec=CliRuntime), wait=True, report=messages.append),
    ):
        pytest.fail("Timed out handoff must not allow shutdown")
    sleep.assert_not_called()
    assert check.call_count == 1
    assert messages == []


@pytest.mark.parametrize("force", [False, True])
def test_stop_refuses_handoff_even_with_force(
    force: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch("gobby.cli.daemon.stop_singleton_gate", return_value=("proceed", None)),
        patch("gobby.cli.daemon.fetch_protected_runs", return_value=[]),
        patch("gobby.cli.runtime.get_cli_runtime", return_value=MagicMock(spec=CliRuntime)),
        patch(
            "gobby.cli.daemon.protect_pending_handoffs",
            side_effect=HandoffShutdownBlocked("unresolved #12332"),
        ),
        patch("gobby.cli.daemon.stop_daemon_util") as stop,
        patch("gobby.cli.daemon.service_stop") as service_stop,
    ):
        result = _do_stop(MagicMock(), docker_flag=False, force=force)
    assert result is False
    stop.assert_not_called()
    service_stop.assert_not_called()
    assert "Refusing to stop: unresolved #12332" in capsys.readouterr().err
