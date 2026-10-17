"""The daemon keeps process-scoped launch environment out of its children (plan 3.1)."""

from __future__ import annotations

import os
import subprocess  # nosec B404 - the test's child is started through gobby.utils.spawn
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.constants import GOBBY_AGENT_API_TOKEN, IDENTITY_ENV_VARS
from gobby.runner import main
from gobby.runner_backend import FRONT_DOOR_SECRET_FD_ENV, front_door_secret
from gobby.runner_init import StartRefusal
from gobby.runner_pid_file import SERVICE_LAUNCH_ENV, SERVICE_NONCE_ENV
from gobby.utils import spawn

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "error,code", [(StartRefusal("schema refused"), 78), (RuntimeError("transient"), 1)]
)
def test_main_classifies_startup_exit(error: Exception, code: int) -> None:
    with (
        patch("gobby.runner.run_gobby", new=MagicMock(return_value=None)),
        patch("asyncio.run", side_effect=error),
        pytest.raises(SystemExit) as exited,
    ):
        main()
    assert exited.value.code == code


def test_main_pops_inherited_agent_token_before_the_runner_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(GOBBY_AGENT_API_TOKEN, "dummy-inherited-token")
    token_at_runner_start: list[str | None] = []

    def record_runner_environment(**_kwargs: object) -> None:
        token_at_runner_start.append(os.environ.get(GOBBY_AGENT_API_TOKEN))

    with (
        patch("gobby.runner.run_gobby", new=record_runner_environment),
        patch("asyncio.run"),
    ):
        main()

    assert token_at_runner_start == [None]
    assert GOBBY_AGENT_API_TOKEN not in os.environ


def test_main_pops_inherited_identity_before_the_runner_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A daemon restarted from a pane must not hand that pane's identity to its children."""
    for name in IDENTITY_ENV_VARS:
        monkeypatch.setenv(name, f"inherited-{name.lower()}")
    identity_at_runner_start: list[set[str]] = []

    def record_runner_environment(**_kwargs: object) -> None:
        identity_at_runner_start.append({name for name in IDENTITY_ENV_VARS if name in os.environ})

    with (
        patch("gobby.runner.run_gobby", new=record_runner_environment),
        patch("asyncio.run"),
    ):
        main()

    assert identity_at_runner_start == [set()]
    assert {name for name in IDENTITY_ENV_VARS if name in os.environ} == set()


def test_main_pops_consumed_native_service_markers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Native admission markers must not reach backend descendants."""
    nonce_path = str(tmp_path / "gobby.pid.service-nonce")
    monkeypatch.setenv(SERVICE_LAUNCH_ENV, "1")
    monkeypatch.setenv(SERVICE_NONCE_ENV, nonce_path)
    service_env = (SERVICE_LAUNCH_ENV, SERVICE_NONCE_ENV)
    marker_at_runner_start: list[set[str]] = []

    def record_runner_environment(**_kwargs: object) -> None:
        marker_at_runner_start.append({name for name in service_env if name in os.environ})

    with (
        patch("gobby.runner_pid_file.convert_or_acquire_service_claim") as admit,
        patch("gobby.runner.run_gobby", new=record_runner_environment),
        patch("asyncio.run"),
    ):
        main()

    admit.assert_not_called()
    assert marker_at_runner_start == [set()]


def test_main_seals_inherited_descriptors_before_the_runner_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spawns keep close_fds=False, so no descriptor the launcher left inheritable reaches them."""
    for name in IDENTITY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv("GOBBY_SUPERVISOR_FD", str(read_fd))
    os.set_inheritable(read_fd, True)
    os.set_inheritable(write_fd, True)
    inheritable_at_runner_start: list[bool] = []
    children: list[subprocess.Popen[bytes]] = []

    def start_a_child(**_kwargs: object) -> None:
        assert os.environ["GOBBY_SUPERVISOR_FD"] == str(read_fd)
        assert not os.get_inheritable(read_fd)
        os.fstat(read_fd)
        inheritable_at_runner_start.append(os.get_inheritable(write_fd))
        children.append(
            spawn.popen(
                [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
            )
        )

    try:
        with (
            patch("gobby.runner.run_gobby", new=start_a_child),
            patch("asyncio.run"),
        ):
            main()
    finally:
        os.close(write_fd)
    try:
        # With the parent's copy closed, EOF means the running child holds none.
        os.set_blocking(read_fd, False)
        end_of_pipe = os.read(read_fd, 1)
    finally:
        os.close(read_fd)
        for child in children:
            child.communicate(b"")

    assert inheritable_at_runner_start == [False]
    assert end_of_pipe == b""
    assert len(children) == 1


def test_main_takes_front_door_secret_from_its_pipe_and_keeps_it_out_of_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Descendants list no secret: it arrives on a pipe and never enters os.environ."""
    secret = "pipe-delivered-front-door-secret"
    read_fd, write_fd = os.pipe()
    os.write(write_fd, secret.encode())
    os.close(write_fd)
    monkeypatch.setenv(FRONT_DOOR_SECRET_FD_ENV, str(read_fd))
    monkeypatch.setattr("gobby.runner_backend._front_door_secret", None, raising=False)
    at_runner_start: list[tuple[str | None, bool, list[str]]] = []

    def record_runner_environment(**_kwargs: object) -> None:
        at_runner_start.append(
            (
                front_door_secret(),
                FRONT_DOOR_SECRET_FD_ENV in os.environ,
                [name for name, value in os.environ.items() if secret in value],
            )
        )

    try:
        with (
            patch("gobby.runner.run_gobby", new=record_runner_environment),
            patch("asyncio.run"),
        ):
            main()
    finally:
        # main closes the pipe once it takes the variable; never close a reused number.
        if FRONT_DOOR_SECRET_FD_ENV in os.environ:
            os.close(read_fd)

    assert at_runner_start == [(secret, False, [])]
