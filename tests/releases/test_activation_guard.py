"""The activation guard refuses spawned agents and passes the operator's pane."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from gobby.cli import cutover as cutover_cli
from gobby.cli import daemon_lifecycle, daemon_start
from gobby.cli.runtime import CliRuntime
from gobby.releases.activation import agent_activation_refusal


def test_spawned_agent_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOBBY_AGENT_RUN_ID", "run-1")

    refusal = agent_activation_refusal()

    assert refusal is not None
    assert refusal.startswith("spawned_agent_refused")


def test_operator_pane_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOBBY_AGENT_RUN_ID", raising=False)

    assert agent_activation_refusal() is None


def test_blank_run_id_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOBBY_AGENT_RUN_ID", "   ")

    assert agent_activation_refusal() is None


@pytest.mark.parametrize("command", ["restart", "cutover"])
def test_activation_starts_the_daemon_it_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    """Refusing the start after the command's own stop would leave no daemon."""
    stopped: list[bool] = []

    def stop(*_args: object, **_kwargs: object) -> bool:
        stopped.append(True)
        return True

    def run_cutover(
        _root: Path,
        _bin_dir: Path,
        *,
        stop_daemon: Callable[[], bool],
        start_daemon: Callable[[], None],
        start_refusal: object,
    ) -> None:
        stop_daemon()
        start_daemon()

    monkeypatch.setattr(
        "gobby.releases.activation.agent_activation_refusal",
        lambda: "spawned_agent_refused: refused after the stop" if stopped else None,
    )
    monkeypatch.setattr(daemon_lifecycle, "restart_start_refusal", lambda *_a, **_k: None)
    monkeypatch.setattr(daemon_lifecycle, "_do_stop", stop)
    monkeypatch.setattr(cutover_cli, "_do_stop", stop)
    monkeypatch.setattr(cutover_cli, "load_bootstrap", MagicMock())
    monkeypatch.setattr(cutover_cli, "node_daemon_refusal", lambda _bootstrap: None)
    monkeypatch.setattr(cutover_cli, "_workspace_root", lambda _path: tmp_path)
    monkeypatch.setattr(cutover_cli, "read_workspace_pin", lambda _root: {})
    monkeypatch.setattr(cutover_cli, "run_cutover", run_cutover)
    start_probe = MagicMock(return_value="probe")
    monkeypatch.setattr(daemon_start, "worktree_daemon_refusal", start_probe)
    entry, args = (
        (daemon_lifecycle.restart, ["--full"])
        if command == "restart"
        else (cutover_cli.cutover, ["--allow-dirty"])
    )

    result = CliRunner().invoke(entry, args, obj=CliRuntime(config_file=None))

    assert stopped == [True], result.output
    assert start_probe.called, result.output
