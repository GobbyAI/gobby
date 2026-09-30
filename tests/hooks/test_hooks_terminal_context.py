"""Tests for normalized hook terminal context."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import psutil
import pytest

from gobby.hooks.terminal_context import (
    clear_codex_seat_index,
    enrich_terminal_context_with_cwd,
    hook_cwd,
    hook_sandbox_enabled,
)
from gobby.sessions.handoff_identity import terminal_contexts_match

pytestmark = pytest.mark.unit


def test_hook_cwd_rejects_whitespace_only_values() -> None:
    assert hook_cwd({"cwd": "   \t"}) is None


def test_hook_cwd_strips_nonblank_values() -> None:
    assert hook_cwd({"cwd": "  /repo/path  "}) == "/repo/path"


def test_enrichment_replaces_blank_terminal_cwd() -> None:
    with patch("gobby.hooks.terminal_context.psutil.Process") as process_cls:
        process = MagicMock()
        process.create_time.return_value = 123.5
        process.name.return_value = "codex"
        process_cls.return_value = process

        result = enrich_terminal_context_with_cwd(
            {"cwd": "  ", "parent_pid": 4321},
            " /repo/path ",
        )

    assert result == {
        "cwd": "/repo/path",
        "parent_pid": 4321,
        "parent_create_time": 123.5,
        "parent_name": "codex",
    }
    process_cls.assert_called_once_with(4321)


def test_enrichment_omits_parent_identity_when_process_is_gone() -> None:
    with patch(
        "gobby.hooks.terminal_context.psutil.Process",
        side_effect=psutil.NoSuchProcess(4321),
    ):
        result = enrich_terminal_context_with_cwd(
            {"parent_pid": 4321, "parent_create_time": 1.0, "tmux_pane": "%1"}, None
        )

    assert result == {"tmux_pane": "%1"}


def _process(
    pid: int, cmdline: list[str], create_time: float, parent: MagicMock | None
) -> MagicMock:
    process = MagicMock()
    process.pid = pid
    process.name.return_value = cmdline[0]
    process.cmdline.return_value = cmdline
    process.create_time.return_value = create_time
    process.parent.return_value = parent
    return process


@pytest.mark.parametrize(
    ("parent_cmdline", "expected_pid", "expected_create_time"),
    [
        (["droid", "--auto", "high"], 100, 1.5),
        (["zsh"], 4321, 9.5),
        (["droid", "exec", "--auto", "high"], 4321, 9.5),
    ],
    ids=["tui-backend", "headless-exec", "exec-under-exec"],
)
def test_droid_identity_is_the_process_that_survives_compress_and_clear(
    parent_cmdline: list[str], expected_pid: int, expected_create_time: float
) -> None:
    parent = _process(100, parent_cmdline, 1.5, None)
    backend = _process(4321, ["droid", "exec", "--input-format", "stream-jsonrpc"], 9.5, parent)
    with patch("gobby.hooks.terminal_context.psutil.Process", return_value=backend):
        result = enrich_terminal_context_with_cwd({"parent_pid": 4321}, None)

    assert result == {
        "parent_pid": expected_pid,
        "parent_create_time": expected_create_time,
        "parent_name": "droid",
    }


HOST_PID = 93395
THREAD_A = "01a0d6a4-4800-7d90-a5d8-3b751ad44281"
THREAD_B = "01a0d723-8589-75d2-adb9-9263ee2bd8bf"
HOST_TERMINAL_ID = "358981a1-df59-43bd-a04e-6cd698238442"
# What ghook builds inside the shared app-server daemon: the daemon's own pid and the
# exited pane whose environment the daemon inherited when it was first launched.
HOST_CONTEXT: dict[str, Any] = {
    "parent_pid": HOST_PID,
    "tty": None,
    "tmux_pane": None,
    "tmux_socket_path": None,
    "tmux_window_id": None,
    "tmux_session": None,
    "term_program": None,
    "gobby_session_id": None,
    "gobby_parent_session_id": None,
    "gobby_agent_run_id": None,
    "gobby_project_id": None,
    "gobby_workflow_name": None,
    "gobby_acp_child": None,
    "gobby_terminal_id": HOST_TERMINAL_ID,
    "gobby_pane_ref": None,
}


@pytest.fixture(autouse=True)
def _fresh_seat_index() -> Iterator[None]:
    clear_codex_seat_index()
    yield
    clear_codex_seat_index()


def _managed_host() -> MagicMock:
    host = _process(
        HOST_PID, ["codex", "app-server", "--listen", "unix://", "--managed-daemon"], 50.0, None
    )
    host.info = {"name": "codex"}
    return host


def _seat(pid: int, thread_id: str, *, tty: str, terminal_id: str, create_time: float) -> MagicMock:
    return _codex_tui(
        pid,
        ["codex", "resume", thread_id, "--yolo"],
        tty=tty,
        terminal_id=terminal_id,
        create_time=create_time,
    )


def _fresh_seat(
    pid: int,
    *,
    tty: str,
    terminal_id: str,
    create_time: float,
    cwd: str = "/repo",
    parent: MagicMock | None = None,
) -> MagicMock:
    """A seat launched as ``codex --yolo <prompt>``: no thread id in argv."""
    seat = _codex_tui(
        pid,
        ["codex", "--yolo", "fix the failing test"],
        tty=tty,
        terminal_id=terminal_id,
        create_time=create_time,
    )
    seat.cwd.return_value = cwd
    seat.parent.return_value = parent
    return seat


def _codex_tui(
    pid: int, cmdline: list[str], *, tty: str, terminal_id: str, create_time: float
) -> MagicMock:
    seat = _process(pid, cmdline, create_time, None)
    seat.info = {"name": "codex"}
    seat.terminal.return_value = tty
    seat.cwd.return_value = "/repo"
    seat.environ.return_value = {
        "GOBBY_HOME": "/Users/josh/.gobby",
        "GOBBY_TERMINAL_ID": terminal_id,
        "TERM": "xterm-256color",
    }
    return seat


def _seat_context(pid: int, *, tty: str, terminal_id: str, create_time: float) -> dict[str, Any]:
    return {
        "cwd": "/repo",
        "parent_pid": pid,
        "parent_create_time": create_time,
        "parent_name": "codex",
        "tty": tty,
        "tmux_pane": None,
        "tmux_socket_path": None,
        "tmux_window_id": None,
        "tmux_session": None,
        "term_program": None,
        "gobby_session_id": None,
        "gobby_parent_session_id": None,
        "gobby_agent_run_id": None,
        "gobby_project_id": None,
        "gobby_workflow_name": None,
        "gobby_acp_child": None,
        "gobby_terminal_id": terminal_id,
        "gobby_pane_ref": None,
    }


@contextmanager
def _process_table(processes: list[MagicMock]) -> Iterator[MagicMock]:
    """Patch psutil so ``Process(pid)`` and ``process_iter`` read one mutable table."""

    def lookup(pid: int) -> MagicMock:
        for process in processes:
            if process.pid == pid:
                return process
        raise psutil.NoSuchProcess(pid)

    with (
        patch("gobby.hooks.terminal_context.psutil.Process", side_effect=lookup),
        patch(
            "gobby.hooks.terminal_context.psutil.process_iter",
            side_effect=lambda **_: list(processes),
        ) as process_iter,
    ):
        yield process_iter


def test_codex_shared_host_hooks_take_identity_from_each_seat_process() -> None:
    seat_a = _seat(22512, THREAD_A, tty="/dev/ttys001", terminal_id="pane-a", create_time=10.0)
    seat_b = _seat(23170, THREAD_B, tty="/dev/ttys002", terminal_id="pane-b", create_time=11.0)

    with _process_table([_managed_host(), seat_a, seat_b]) as process_iter:
        context_a = enrich_terminal_context_with_cwd(
            dict(HOST_CONTEXT), "/repo", external_id=THREAD_A
        )
        context_b = enrich_terminal_context_with_cwd(
            dict(HOST_CONTEXT), "/repo", external_id=THREAD_B
        )

    assert context_a == _seat_context(
        22512, tty="/dev/ttys001", terminal_id="pane-a", create_time=10.0
    )
    assert context_b == _seat_context(
        23170, tty="/dev/ttys002", terminal_id="pane-b", create_time=11.0
    )
    assert not terminal_contexts_match(context_a, context_b)
    assert process_iter.call_count == 1


def test_codex_shared_host_without_a_seat_process_records_no_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        _process_table([_managed_host()]),
        caplog.at_level(logging.WARNING, logger="gobby.hooks.terminal_context"),
    ):
        result = enrich_terminal_context_with_cwd(dict(HOST_CONTEXT), "/repo", external_id=THREAD_A)

    assert result == {"cwd": "/repo"}
    assert THREAD_A in caplog.text
    assert str(HOST_PID) in caplog.text


def test_gobby_spawned_codex_app_server_keeps_its_own_identity() -> None:
    worker = _process(49478, ["codex", "app-server"], 7.0, None)

    with _process_table([worker]) as process_iter:
        result = enrich_terminal_context_with_cwd(
            {"parent_pid": 49478, "gobby_terminal_id": "pane-w", "gobby_agent_run_id": "run-1"},
            "/repo",
            external_id=THREAD_A,
        )

    assert result == {
        "cwd": "/repo",
        "parent_pid": 49478,
        "parent_create_time": 7.0,
        "parent_name": "codex",
        "gobby_terminal_id": "pane-w",
        "gobby_agent_run_id": "run-1",
    }
    process_iter.assert_not_called()


def test_seat_index_rescans_when_the_recorded_seat_process_is_gone() -> None:
    table = [
        _managed_host(),
        _seat(22512, THREAD_A, tty="/dev/ttys001", terminal_id="pane-a", create_time=10.0),
    ]

    with _process_table(table) as process_iter:
        enrich_terminal_context_with_cwd(dict(HOST_CONTEXT), "/repo", external_id=THREAD_A)
        enrich_terminal_context_with_cwd(dict(HOST_CONTEXT), "/repo", external_id=THREAD_A)
        assert process_iter.call_count == 1

        table[:] = [
            _managed_host(),
            _seat(30001, THREAD_A, tty="/dev/ttys004", terminal_id="pane-d", create_time=20.0),
        ]
        result = enrich_terminal_context_with_cwd(dict(HOST_CONTEXT), "/repo", external_id=THREAD_A)

    assert process_iter.call_count == 2
    assert result == _seat_context(
        30001, tty="/dev/ttys004", terminal_id="pane-d", create_time=20.0
    )


def test_unresolved_thread_does_not_rescan_on_every_hook() -> None:
    fresh_seat = _process(31000, ["codex", "--yolo"], 12.0, None)
    fresh_seat.info = {"name": "codex"}

    with _process_table([_managed_host(), fresh_seat]) as process_iter:
        first = enrich_terminal_context_with_cwd(dict(HOST_CONTEXT), "/repo", external_id=THREAD_A)
        second = enrich_terminal_context_with_cwd(dict(HOST_CONTEXT), "/repo", external_id=THREAD_A)

    assert first == second == {"cwd": "/repo"}
    assert process_iter.call_count == 1


def test_seat_inside_tmux_carries_its_pane_identity() -> None:
    seat = _seat(22512, THREAD_A, tty="/dev/ttys001", terminal_id="pane-a", create_time=10.0)
    seat.environ.return_value = {
        **seat.environ.return_value,
        "TMUX": "/tmp/tmux-501/default,123,0",
        "TMUX_PANE": "%73",
        "TERM_PROGRAM": "tmux",
    }

    with (
        _process_table([_managed_host(), seat]),
        patch(
            "gobby.hooks.terminal_context.query_tmux_identity", return_value=("@3", "main")
        ) as query_tmux_identity,
    ):
        result = enrich_terminal_context_with_cwd(dict(HOST_CONTEXT), "/repo", external_id=THREAD_A)

    query_tmux_identity.assert_called_once_with("/tmp/tmux-501/default", "%73")
    assert result == {
        **_seat_context(22512, tty="/dev/ttys001", terminal_id="pane-a", create_time=10.0),
        "tmux_pane": "%73",
        "tmux_socket_path": "/tmp/tmux-501/default",
        "tmux_window_id": "@3",
        "tmux_session": "main",
        "term_program": "tmux",
    }


# A fresh seat's thread id is a UUIDv7 minted a second or two after the TUI starts.
FRESH_MINTED_AT = 1_790_000_000.5
FRESH_THREAD = "01a0db67-65f3-7961-a61c-c959158deffb"


def _uuid7(minted_at: float) -> str:
    """A UUIDv7 minted at ``minted_at`` UNIX seconds, as Codex mints thread ids."""
    milliseconds = int(minted_at * 1000)
    return str(UUID(int=(milliseconds << 80) | (7 << 76) | (0x2 << 62) | 0x1234_5678_9ABC))


def test_uuid7_helper_decodes_a_real_codex_thread_id() -> None:
    # Thread 01a0db67-... was minted at 2026-09-26T01:49:41.747Z on a live seat.
    assert _uuid7(1_790_387_381.747) == "01a0db67-65f3-7000-8000-123456789abc"
    assert (UUID(FRESH_THREAD).int >> 80) / 1000.0 == 1_790_387_381.747


def test_fresh_seat_adopts_the_tui_created_just_before_its_thread() -> None:
    thread = _uuid7(FRESH_MINTED_AT)
    seat = _fresh_seat(
        71386, tty="/dev/ttys001", terminal_id="pane-a", create_time=FRESH_MINTED_AT - 1.5
    )

    with _process_table([_managed_host(), seat]):
        result = enrich_terminal_context_with_cwd(dict(HOST_CONTEXT), "/repo", external_id=thread)

    assert result == _seat_context(
        71386, tty="/dev/ttys001", terminal_id="pane-a", create_time=FRESH_MINTED_AT - 1.5
    )


def test_fresh_seat_prefers_the_newest_earlier_tui() -> None:
    thread = _uuid7(FRESH_MINTED_AT)
    older = _fresh_seat(
        71000, tty="/dev/ttys001", terminal_id="pane-a", create_time=FRESH_MINTED_AT - 20.0
    )
    newer = _fresh_seat(
        71386, tty="/dev/ttys002", terminal_id="pane-b", create_time=FRESH_MINTED_AT - 1.5
    )

    with _process_table([_managed_host(), newer, older]):
        result = enrich_terminal_context_with_cwd(dict(HOST_CONTEXT), "/repo", external_id=thread)

    assert result == _seat_context(
        71386, tty="/dev/ttys002", terminal_id="pane-b", create_time=FRESH_MINTED_AT - 1.5
    )


def test_fresh_seat_ignores_tuis_outside_the_creation_window(
    caplog: pytest.LogCaptureFixture,
) -> None:
    thread = _uuid7(FRESH_MINTED_AT)
    later = _fresh_seat(
        71500, tty="/dev/ttys001", terminal_id="pane-a", create_time=FRESH_MINTED_AT + 0.5
    )
    stale = _fresh_seat(
        70000, tty="/dev/ttys002", terminal_id="pane-b", create_time=FRESH_MINTED_AT - 61.0
    )

    with (
        _process_table([_managed_host(), stale, later]),
        caplog.at_level(logging.WARNING, logger="gobby.hooks.terminal_context"),
    ):
        result = enrich_terminal_context_with_cwd(dict(HOST_CONTEXT), "/repo", external_id=thread)

    assert result == {"cwd": "/repo"}
    assert thread in caplog.text


def test_fresh_seat_ignores_other_cwds_and_codex_parented_helpers() -> None:
    thread = _uuid7(FRESH_MINTED_AT)
    eligible = _fresh_seat(
        71000, tty="/dev/ttys001", terminal_id="pane-a", create_time=FRESH_MINTED_AT - 10.0
    )
    elsewhere = _fresh_seat(
        71386,
        tty="/dev/ttys002",
        terminal_id="pane-b",
        create_time=FRESH_MINTED_AT - 1.5,
        cwd="/elsewhere",
    )
    helper = _fresh_seat(
        71400,
        tty="/dev/ttys001",
        terminal_id="pane-a",
        create_time=FRESH_MINTED_AT - 0.5,
        parent=eligible,
    )

    with _process_table([_managed_host(), eligible, elsewhere, helper]):
        result = enrich_terminal_context_with_cwd(dict(HOST_CONTEXT), "/repo", external_id=thread)

    assert result == _seat_context(
        71000, tty="/dev/ttys001", terminal_id="pane-a", create_time=FRESH_MINTED_AT - 10.0
    )


def test_fresh_seat_never_adopts_a_resumed_seat() -> None:
    thread = _uuid7(FRESH_MINTED_AT)
    resumed = _seat(
        22512, THREAD_A, tty="/dev/ttys001", terminal_id="pane-a", create_time=FRESH_MINTED_AT - 1.0
    )

    with _process_table([_managed_host(), resumed]):
        result = enrich_terminal_context_with_cwd(dict(HOST_CONTEXT), "/repo", external_id=thread)

    assert result == {"cwd": "/repo"}


def test_fresh_seat_adoption_is_cached_without_a_rescan() -> None:
    thread = _uuid7(FRESH_MINTED_AT)
    seat = _fresh_seat(
        71386, tty="/dev/ttys001", terminal_id="pane-a", create_time=FRESH_MINTED_AT - 1.5
    )
    table = [_managed_host(), seat]

    with _process_table(table) as process_iter:
        first = enrich_terminal_context_with_cwd(dict(HOST_CONTEXT), "/repo", external_id=thread)
        second = enrich_terminal_context_with_cwd(dict(HOST_CONTEXT), "/repo", external_id=thread)
        assert process_iter.call_count == 1

        table[:] = [_managed_host()]
        third = enrich_terminal_context_with_cwd(dict(HOST_CONTEXT), "/repo", external_id=thread)

    expected = _seat_context(
        71386, tty="/dev/ttys001", terminal_id="pane-a", create_time=FRESH_MINTED_AT - 1.5
    )
    assert first == second == expected
    assert third == {"cwd": "/repo"}
    assert process_iter.call_count == 2


# The successor TUI for #14730 sat at a login prompt for 62 s before minting its thread.
SLOW_SEAT_STARTED_AT = FRESH_MINTED_AT - 188.0


def test_new_thread_adopts_the_only_unowned_seat_past_the_fresh_window() -> None:
    thread = _uuid7(FRESH_MINTED_AT)
    owned = _fresh_seat(
        70100, tty="/dev/ttys004", terminal_id="pane-b", create_time=SLOW_SEAT_STARTED_AT - 30
    )
    slow = _fresh_seat(
        71386, tty="/dev/ttys001", terminal_id="pane-a", create_time=SLOW_SEAT_STARTED_AT
    )
    checked: list[tuple[int, float]] = []

    def available(pid: int, create_time: float) -> bool:
        checked.append((pid, create_time))
        return pid != 70100

    with _process_table([_managed_host(), owned, slow]):
        hook_only = enrich_terminal_context_with_cwd(
            dict(HOST_CONTEXT), "/repo", external_id=thread
        )
        adopted = enrich_terminal_context_with_cwd(
            dict(HOST_CONTEXT), "/repo", external_id=thread, seat_available=available
        )
        later_hook = enrich_terminal_context_with_cwd(
            dict(HOST_CONTEXT), "/repo", external_id=thread
        )

    expected = _seat_context(
        71386, tty="/dev/ttys001", terminal_id="pane-a", create_time=SLOW_SEAT_STARTED_AT
    )
    assert hook_only == {"cwd": "/repo"}
    assert adopted == later_hook == expected
    assert sorted(checked) == [(70100, SLOW_SEAT_STARTED_AT - 30), (71386, SLOW_SEAT_STARTED_AT)]


@pytest.mark.parametrize(
    ("second_seat_owned", "cwd"),
    [(False, "/repo"), (True, None)],
    ids=["two-unowned-seats", "unknown-cwd"],
)
def test_new_thread_refuses_an_ambiguous_unowned_seat(
    second_seat_owned: bool, cwd: str | None
) -> None:
    thread = _uuid7(FRESH_MINTED_AT)
    first = _fresh_seat(
        70100, tty="/dev/ttys004", terminal_id="pane-b", create_time=SLOW_SEAT_STARTED_AT - 30
    )
    second = _fresh_seat(
        71386, tty="/dev/ttys001", terminal_id="pane-a", create_time=SLOW_SEAT_STARTED_AT
    )

    with _process_table([_managed_host(), first, second]):
        result = enrich_terminal_context_with_cwd(
            dict(HOST_CONTEXT),
            cwd,
            external_id=thread,
            seat_available=lambda pid, _: pid != second.pid or not second_seat_owned,
        )

    assert result == ({"cwd": cwd} if cwd else {})


def test_new_thread_never_adopts_an_owned_or_later_seat() -> None:
    thread = _uuid7(FRESH_MINTED_AT)
    owned = _fresh_seat(
        70100, tty="/dev/ttys004", terminal_id="pane-b", create_time=SLOW_SEAT_STARTED_AT
    )
    later = _fresh_seat(
        71500, tty="/dev/ttys001", terminal_id="pane-a", create_time=FRESH_MINTED_AT + 0.5
    )
    checked: list[int] = []

    def available(pid: int, _create_time: float) -> bool:
        checked.append(pid)
        return pid != 70100

    with _process_table([_managed_host(), owned, later]):
        result = enrich_terminal_context_with_cwd(
            dict(HOST_CONTEXT), "/repo", external_id=thread, seat_available=available
        )

    assert result == {"cwd": "/repo"}
    assert checked == [70100]


# #23049 option A: a pane locks only under Gobby's SRT, which the launch and
# run records carry. The hook records only a launcher's explicit bool; a
# provider's own sandbox flags or permission mode never read as locked.
@pytest.mark.parametrize(
    ("input_data", "expected"),
    [
        ({"sandbox_enabled": True}, True),
        ({"sandbox_enabled": False}, False),
        ({}, None),
        ({"sandbox_enabled": "yes"}, None),
        ({"sandbox_enabled": 1}, None),
        ({"permission_mode": "bypassPermissions"}, None),
    ],
)
def test_hook_sandbox_enabled_reports_only_the_launcher_bool(
    input_data: dict[str, Any], expected: bool | None
) -> None:
    assert hook_sandbox_enabled(input_data) is expected
