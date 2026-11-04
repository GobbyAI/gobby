"""The foreground command a terminal row reports for the pane label ladder (#22536)."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import psutil
import pytest

from gobby.terminals.foreground import (
    _process_snapshot,
    command_name,
    foreground_child_executables,
    foreground_commands,
    foreground_node_script,
    process_shell,
    shell_cwds,
    shell_pid,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("argv", "executable", "expected"),
    [
        (["/usr/local/bin/node", "/pkg/cli.js"], "/usr/local/bin/node", "/pkg/cli.js"),
        (["/usr/local/bin/node", "relative.js"], "/usr/local/bin/node", None),
        (["/usr/local/bin/node", "--eval", "code"], "/usr/local/bin/node", None),
        (["/usr/local/bin/node"], "/usr/local/bin/node", None),
        (["/usr/local/bin/node", "/pkg/cli.js"], "/bin/other", None),
    ],
)
def test_node_script_is_read_from_actual_foreground_group(
    argv: list[str], executable: str, expected: str | None
) -> None:
    with (
        patch(
            "gobby.terminals.foreground._process_snapshot",
            return_value={
                10: (20, "/bin/zsh"),
                20: (20, "/usr/local/bin/node"),
            },
        ),
        patch("gobby.terminals.foreground.psutil.Process") as process,
    ):
        process.return_value.cmdline.return_value = argv
        process.return_value.exe.return_value = executable
        assert foreground_node_script(10) == expected
        process.assert_called_once_with(20)


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
    with patch("gobby.terminals.foreground.spawn.run", return_value=_ps(PS_TABLE)):
        snapshot = _process_snapshot()

    assert snapshot[37904] == (37904, "-zsh")
    assert snapshot[83724] == (86413, "/bin/zsh")
    assert snapshot[535] == (0, "/Applications/Microsoft Outlook.app/Contents/MacOS/Outlook")


def test_snapshot_skips_lines_it_cannot_parse() -> None:
    noise = "not a process row\n   \n12 notanumber sh\n77 77 bash\n"
    with patch("gobby.terminals.foreground.spawn.run", return_value=_ps(noise)):
        snapshot = _process_snapshot()

    assert snapshot == {77: (77, "bash")}


def test_an_idle_shell_reports_itself_and_a_running_job_reports_the_job() -> None:
    with patch("gobby.terminals.foreground.spawn.run", return_value=_ps(PS_TABLE)):
        resolved = foreground_commands({"idle": 37904, "busy": 83724})

    assert resolved == {"idle": "zsh", "busy": "claude"}


def test_a_shell_with_no_controlling_terminal_has_no_command() -> None:
    with patch("gobby.terminals.foreground.spawn.run", return_value=_ps(PS_TABLE)):
        resolved = foreground_commands({"daemonized": 90001})

    assert resolved == {}


def test_one_unresolvable_row_never_blanks_the_others() -> None:
    with patch("gobby.terminals.foreground.spawn.run", return_value=_ps(PS_TABLE)) as run:
        resolved = foreground_commands({"gone": 54321, "absurd": 999999, "live": 37904})

    assert resolved == {"live": "zsh"}
    # One `ps` for the whole page, and it never names the pids, so a stale id
    # cannot make the platform reject the request.
    run.assert_called_once()
    assert "-p" not in run.call_args.args[0]


def test_no_shell_pids_means_no_subprocess_at_all() -> None:
    with patch("gobby.terminals.foreground.spawn.run") as run:
        assert foreground_commands({}) == {}
        assert foreground_commands({"unstarted": 0}) == {}

    run.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [OSError("ps missing"), subprocess.TimeoutExpired(cmd="ps", timeout=2.0)],
    ids=["ps-missing", "ps-timeout"],
)
def test_a_failed_lookup_yields_no_commands_rather_than_raising(failure: Exception) -> None:
    with patch("gobby.terminals.foreground.spawn.run", side_effect=failure):
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


@pytest.mark.parametrize(
    ("process", "shell"),
    [
        ({"pgid": 42, "host_terminal_id": "ht-1", "shell": "zsh"}, "zsh"),
        (None, None),
        ({"pgid": 42}, None),
        ({"shell": ""}, None),
        ({"shell": None}, None),
        ({"shell": 7}, None),
    ],
    ids=["recorded", "absent", "unrecorded", "empty", "null", "not-a-string"],
)
def test_process_shell_reads_the_recorded_basename(
    process: dict[str, Any] | None, shell: str | None
) -> None:
    assert process_shell(Row(process=process)) == shell


def test_a_live_shell_reports_its_working_directory() -> None:
    assert shell_cwds({"me": os.getpid()}) == {"me": os.getcwd()}


def test_an_unreadable_or_unstarted_shell_has_no_working_directory() -> None:
    denied = psutil.AccessDenied(pid=4242)
    with patch("gobby.terminals.foreground.psutil.Process", side_effect=denied) as process:
        assert shell_cwds({"denied": 4242, "unstarted": 0}) == {}

    process.assert_called_once_with(4242)
    with patch(
        "gobby.terminals.foreground.psutil.Process", side_effect=psutil.NoSuchProcess(pid=54321)
    ):
        assert shell_cwds({"gone": 54321}) == {}


@dataclass
class _Child:
    pid: int
    path: str
    error: Exception | None = None

    def exe(self) -> str:
        if self.error is not None:
            raise self.error
        return self.path


def test_child_executables_keep_direct_children_that_share_the_launchers_group() -> None:
    claude = "/Users/josh/.local/share/claude/versions/2.1.318"
    groups = {20: 20, 31: 20, 32: 99, 33: 20, 34: 20}
    children = [
        _Child(31, claude),
        _Child(32, "/bin/sh"),  # left the launcher's group
        _Child(33, "/bin/zsh", error=psutil.AccessDenied(pid=33)),
        _Child(34, "/bin/zsh"),  # exited before its group was read
    ]

    def getpgid(pid: int) -> int:
        if pid == 34:
            raise ProcessLookupError(pid)
        return groups[pid]

    with (
        patch("gobby.terminals.foreground.os.getpgid", side_effect=getpgid),
        patch("gobby.terminals.foreground.psutil.Process") as process,
    ):
        process.return_value.children.return_value = children

        assert foreground_child_executables(20) == [claude]

    process.assert_called_once_with(20)
    process.return_value.children.assert_called_once_with(recursive=False)


def test_child_executables_are_empty_when_the_launcher_is_gone() -> None:
    with (
        patch("gobby.terminals.foreground.os.getpgid", return_value=20),
        patch(
            "gobby.terminals.foreground.psutil.Process",
            side_effect=psutil.NoSuchProcess(pid=20),
        ),
    ):
        assert foreground_child_executables(20) == []


def test_child_executables_are_empty_when_the_launchers_group_is_unreadable() -> None:
    with (
        patch("gobby.terminals.foreground.os.getpgid", side_effect=ProcessLookupError(20)),
        patch("gobby.terminals.foreground.psutil.Process") as process,
    ):
        assert foreground_child_executables(20) == []

    process.assert_not_called()
