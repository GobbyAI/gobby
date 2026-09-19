"""The foreground command a terminal row reports for the pane label ladder (#22536)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from gobby.terminals.foreground import (
    _process_snapshot,
    command_name,
    foreground_commands,
    shell_pid,
)

pytestmark = pytest.mark.unit

# pid, tpgid, comm -- the three columns `ps -A -o pid=,tpgid=,comm=` prints.
PS_TABLE = """\
    1    0 /sbin/launchd
  535    0 /Applications/Microsoft Outlook.app/Contents/MacOS/Outlook
37904 37904 -zsh
83724 86413 /bin/zsh
86413 86413 claude
90001    0 /usr/libexec/logd
"""


@dataclass
class Row:
    """The part of a terminal row the resolver reads."""

    id: str = "terminal-1"
    process: dict[str, Any] | None = field(default_factory=lambda: {"pgid": 37904})


def _ps(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["ps"], returncode=returncode, stdout=stdout, stderr="")


def test_command_name_strips_paths_and_the_login_shell_dash() -> None:
    assert command_name("/bin/zsh") == "zsh"
    assert command_name("-zsh") == "zsh"
    assert command_name("  claude  ") == "claude"
    assert command_name("cargo") == "cargo"


def test_snapshot_parses_commands_that_contain_spaces() -> None:
    with patch("gobby.terminals.foreground.subprocess.run", return_value=_ps(PS_TABLE)):
        snapshot = _process_snapshot()

    assert snapshot[37904] == (37904, "-zsh")
    assert snapshot[83724] == (86413, "/bin/zsh")
    assert snapshot[535] == (0, "/Applications/Microsoft Outlook.app/Contents/MacOS/Outlook")


def test_snapshot_skips_lines_it_cannot_parse() -> None:
    noise = "not a process row\n   \n12 notanumber sh\n77 77 bash\n"
    with patch("gobby.terminals.foreground.subprocess.run", return_value=_ps(noise)):
        snapshot = _process_snapshot()

    assert snapshot == {77: (77, "bash")}


def test_an_idle_shell_reports_itself_and_a_running_job_reports_the_job() -> None:
    with patch("gobby.terminals.foreground.subprocess.run", return_value=_ps(PS_TABLE)):
        resolved = foreground_commands({"idle": 37904, "busy": 83724})

    assert resolved == {"idle": "zsh", "busy": "claude"}


def test_a_shell_with_no_controlling_terminal_has_no_command() -> None:
    with patch("gobby.terminals.foreground.subprocess.run", return_value=_ps(PS_TABLE)):
        resolved = foreground_commands({"daemonized": 90001})

    assert resolved == {}


def test_one_unresolvable_row_never_blanks_the_others() -> None:
    with patch("gobby.terminals.foreground.subprocess.run", return_value=_ps(PS_TABLE)) as run:
        resolved = foreground_commands({"gone": 54321, "absurd": 999999, "live": 37904})

    assert resolved == {"live": "zsh"}
    # One `ps` for the whole page, and it never names the pids, so a stale id
    # cannot make the platform reject the request.
    run.assert_called_once()
    assert "-p" not in run.call_args.args[0]


def test_no_shell_pids_means_no_subprocess_at_all() -> None:
    with patch("gobby.terminals.foreground.subprocess.run") as run:
        assert foreground_commands({}) == {}
        assert foreground_commands({"unstarted": 0}) == {}

    run.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [OSError("ps missing"), subprocess.TimeoutExpired(cmd="ps", timeout=2.0)],
    ids=["ps-missing", "ps-timeout"],
)
def test_a_failed_lookup_yields_no_commands_rather_than_raising(failure: Exception) -> None:
    with patch("gobby.terminals.foreground.subprocess.run", side_effect=failure):
        assert foreground_commands({"live": 37904}) == {}


def test_shell_pid_reads_the_native_process_record() -> None:
    assert shell_pid(Row()) == 37904
    assert shell_pid(Row(process={"pgid": 42, "host_terminal_id": "ht-1"})) == 42


@pytest.mark.parametrize(
    "process",
    [None, {}, {"pgid": None}, {"pgid": 0}, {"pgid": -3}, {"pgid": True}, {"pgid": "37904"}],
    ids=["absent", "empty", "null", "zero", "negative", "bool", "string"],
)
def test_shell_pid_rejects_a_row_without_a_usable_pid(process: dict[str, Any] | None) -> None:
    assert shell_pid(Row(process=process)) is None
