"""Daemon spawns start through posix_spawn (#22815)."""

from __future__ import annotations

import ast
import asyncio
import os
import re
import subprocess
import sys
from collections.abc import KeysView
from pathlib import Path
from typing import Any

import pytest

from gobby.utils import spawn

pytestmark = pytest.mark.unit

_SRC = Path(spawn.__file__).resolve().parents[1]
_SPAWN_CALLS = frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
        "asyncio.subprocess.create_subprocess_exec",
        "asyncio.subprocess.create_subprocess_shell",
        "concurrent.futures.ProcessPoolExecutor",
        "multiprocessing.Process",
        "multiprocessing.Pool",
        "os.system",
        "os.popen",
        "os.fork",
        "os.forkpty",
        *(f"os.spawn{kind}" for kind in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")),
        "pty.spawn",
        "pty.fork",
    }
)
# Event-loop methods, whatever the loop expression: loop.subprocess_exec(...).
_LOOP_SPAWNS = frozenset({"subprocess_exec", "subprocess_shell"})
# Modules whose names the scan resolves through import aliases.
_SPAWN_MODULES = frozenset(
    {
        "subprocess",
        "os",
        "pty",
        "asyncio",
        "asyncio.subprocess",
        "concurrent.futures",
        "multiprocessing",
    }
)
# Every call in _SPAWN_CALLS names its module or function in the source text, so
# the scan parses only the files that could hold one.
_MAY_SPAWN = re.compile(
    r"subprocess|\bsystem\b|\bpopen\b|\bfork(?:pty)?\b|\bpty\b|\bspawn[lv]"
    r"|ProcessPool|multiprocessing"
)
# CLI commands, processes of their own (the stdio MCP proxy, Chrome's supervisor)
# and bundled templates. Daemon code also calls into some of these modules, so
# every module a daemon module imports, transitively, is scanned as well.
_NOT_DAEMON = (
    "cli/",
    "install/shared/",
    "guard_set_g.py",
    "test_types/",
    "ui_exposure.py",
    "mcp_proxy/daemon_control.py",
    "mcp_proxy/stdio",
    "mcp_proxy/transports/chrome_supervisor.py",
)
# Scanned sites that still fork, keyed path::qualname, each with its reason: the
# daemon sites that need an option neither helper gives (spawn.create_session_exec
# starts a session leader with no cwd or stdin and reads its output at exit), and
# the CLI-only functions of modules the daemon imports. A key the scan no longer
# finds fails the lint.
_MUST_FORK = {
    "cli/daemon.py::_launch_direct_runner": (
        "own session and pass_fds for the runner claim; `gobby start` only"
    ),
    "cli/install_setup_impeccable.py::_detect_node": (
        "`gobby install` only; the daemon calls just inspect_impeccable_installation"
    ),
    "cli/install_setup_impeccable.py::_run_owned_process": (
        "own session for npm and the Chrome download; `gobby install` only"
    ),
    "cli/installers/postgres.py::_install_docker": (
        "`gobby install` only; the daemon calls just get_postgres_status"
    ),
    "cli/installers/postgres.py::_wait_for_pg_isready": (
        "`gobby install` only; the daemon calls just get_postgres_status"
    ),
    "cli/installers/service.py::install_service_macos": "`gobby install` only",
    "cli/installers/service.py::enable_service_macos": "CLI service commands only",
    "cli/installers/service.py::_launchctl_bootout": (
        "CLI service commands and the restart helper process only"
    ),
    "cli/installers/service.py::_macos_restart": (
        "the restart helper process, after the daemon has exited"
    ),
    "cli/installers/service.py::_get_service_status_macos": (
        "once per admin restart, to ask whether launchd owns the daemon"
    ),
    "cli/installers/service_linux.py::_get_service_status_linux": (
        "once per admin restart, to ask whether systemd owns the daemon"
    ),
    "cli/installers/service_linux.py::_check_linger": (
        "part of the Linux service status, once per admin restart"
    ),
    "cli/installers/service_linux.py::_linux_restart": (
        "the restart helper process, after the daemon has exited"
    ),
    "cli/installers/service_linux.py::install_service_linux": "`gobby install` only",
    "cli/installers/service_linux.py::uninstall_service_linux": "CLI service commands only",
    "cli/installers/service_linux.py::enable_service_linux": "CLI service commands only",
    "cli/installers/service_linux.py::disable_service_linux": "CLI service commands only",
    "cli/installers/service_windows.py::_run_schtasks": (
        "Windows only, where subprocess starts processes without fork"
    ),
    "cli/installers/service_common.py::_ensure_cli_on_path": "`gobby install` only",
    "cli/install_setup.py::_run_npm_install": "`gobby install` only",
    "cli/install_setup_gdaemon.py::_codesign": "`gobby install` only",
    "cli/install_setup_gdaemon.py::_install_from_workspace": "`gobby install` only",
    "cli/install_setup_gdaemon.py::_probe_identity": "`gobby install` only",
    "cli/install_setup_gdaemon.py::_probe_version": "`gobby install` only",
    "cli/install_setup_srt.py::_install_srt_runtime": "`gobby install` only",
    "cli/installers/falkor.py::_install_falkordb_locked": "`gobby install` only",
    "cli/installers/falkor.py::_wait_for_health_async": "`gobby install` only",
    "cli/installers/tmux_config.py::_apply_to_running_server": "`gobby install` only",
    "cli/installers/docker_guard.py::<module>": (
        "an identity reference that recognizes the real runner; never called"
    ),
    "cli/_daemon_services.py::_run_compose_command": "managed services of `gobby start`/`stop` only",
    "cli/_daemon_services.py::_run_compose_up": "managed services of `gobby start` only",
    "cli/_daemon_services.py::_stop_managed_services_locked": "managed services of `gobby stop` only",
    "cli/_daemon_services.py::_terminate_compose_process": (
        "managed services of `gobby start`/`stop` only"
    ),
    "cli/datastores.py::_snapshot_compose_running": "`gobby datastores expose` only",
    "cli/datastores.py::_tailscale_ipv4_addresses": "`gobby datastores expose` only",
    "cli/utils_ui.py::spawn_ui_server": (
        "own session: the UI dev server outlives the daemon; once per start in UI dev mode"
    ),
    "ui_exposure.py::_run_json": "tailscale for the start, ui, install and uninstall commands only",
    "ui_exposure.py::_run_mutation": (
        "tailscale for the start, ui, install and uninstall commands only"
    ),
    "ai/_text_generation_adapters.py::_run_cli_text_generation_command": (
        "own session with a cwd and stdin: a timeout killpg()s the CLI's children;"
        " once per generation, off the hook path"
    ),
    "ai/_text_generation_adapters.py::DroidCLITextGenerateAdapter.generate": (
        "own session with a cwd: a timeout killpg()s droid's children; once per generation"
    ),
    "mcp_proxy/tools/merge_landscape.py::register_merge_landscape_tools.verify_in_worktree": (
        "own session with a cwd: a timeout killpg()s the verify command's children;"
        " once per merge check"
    ),
    "runner_gate.py::acquire_runner_gate": (
        "own session, apart from the terminal's signals; once per start, before the daemon grows"
    ),
    "servers/routes/admin/_lifecycle.py::_run_direct_restart_helper": (
        "own session: the restart helper outlives the daemon it stops"
    ),
    "servers/routes/admin/_lifecycle.py::_spawn_restart_helper": (
        "own session: the restart helper outlives the daemon it stops"
    ),
    "servers/websocket/chat/backends/agy.py::AgyWebChatBackend.attach_session": (
        "own session with live ACP pipes: detach killpg()s agy's children; once per chat attach"
    ),
    "servers/websocket/chat/backends/droid.py::DroidWebChatBackend.attach_session": (
        "own session with live ACP pipes: detach killpg()s droid's children; once per chat attach"
    ),
    "skills/materialization.py::_run_owned_subprocess": (
        "own session and pass_fds for the ownership pipe; once per skill materialization"
    ),
    "tasks/transcript_evidence_pool.py::_get_pool": (
        "the spawn context starts its one worker and the resource tracker through"
        " multiprocessing's fork_exec, once per pool creation"
    ),
    "terminals/host_manager.py::TerminalHostManager._spawn_host_process": (
        "own session: the gterm host outlives daemon restarts; once per host start"
    ),
    "utils/daemon_git.py::_spawn_git": (
        "the Popen session fallback only runs where spawn.can_posix_spawn() is false"
    ),
}


def _spawn_references(tree: ast.Module) -> list[tuple[str, str]]:
    """Return (qualname, dotted name) for each process-creating call or reference.

    A spawn function passed as a value (``asyncio.to_thread(subprocess.run, ...)``,
    a ``runner=subprocess.run`` default) forks when called, and so does code
    handed the ``subprocess`` module itself.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _SPAWN_MODULES:
            for name in node.names:
                aliases[name.asname or name.name] = f"{node.module}.{name.name}"
        elif isinstance(node, ast.Import):
            for name in node.names:
                if name.name in _SPAWN_MODULES and name.asname:
                    aliases[name.asname] = name.name
    annotations = {
        id(part)
        for node in ast.walk(tree)
        for annotation in (getattr(node, "annotation", None), getattr(node, "returns", None))
        if isinstance(annotation, ast.expr)
        for part in ast.walk(annotation)
    }
    found: list[tuple[str, str]] = []

    def visit(node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, [*scope, child.name])
                continue
            # The whole dotted name, outside annotations and generics like Popen[bytes].
            if (
                isinstance(child, (ast.Name, ast.Attribute))
                and not isinstance(node, ast.Attribute)
                and not (isinstance(node, ast.Subscript) and child is node.value)
                and id(child) not in annotations
            ):
                # __import__("subprocess").Popen names the module inline.
                dotted = re.sub(r"^__import__\('(\w+)'\)", r"\1", ast.unparse(child))
                head, _, tail = dotted.partition(".")
                resolved = aliases.get(head, head) + (f".{tail}" if tail else "")
                if (
                    resolved in _SPAWN_CALLS
                    or resolved in {"subprocess", "pty"}
                    or resolved.rpartition(".")[2] in _LOOP_SPAWNS
                ):
                    found.append((".".join(scope) or "<module>", resolved))
            visit(child, scope)

    visit(tree, [])
    return found


# Import statements, including lazy ones inside functions and parenthesized name lists.
_IMPORT = re.compile(
    r"^[ \t]*(?:from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+(\([^)]*\)|[^\n]*)|import[ \t]+([^\n]*))",
    re.M,
)


def _imported_modules(relative: str, source: str, modules: KeysView[str]) -> set[str]:
    """Modules whose code ``relative`` imports; ``from pkg import sub`` reaches pkg/sub, not pkg."""

    def files(stem: str) -> set[str]:
        return {f"{stem}.py", f"{stem}/__init__.py"} & modules

    def names(listing: str) -> list[str]:
        cleaned = re.sub(r"#[^\n]*", "", listing).strip("()")
        return [part.split()[0] for part in cleaned.split(",") if part.strip()]

    package = relative.split("/")[:-1]
    reached: set[str] = set()
    for dots, module, imported, plain in _IMPORT.findall(source):
        if plain:
            for name in names(plain):
                if name.startswith("gobby."):
                    reached |= files(name.removeprefix("gobby.").replace(".", "/"))
            continue
        parts = module.split(".") if module else []
        if dots:
            stem = "/".join([*package[: len(package) - len(dots) + 1], *parts])
        elif parts[:1] == ["gobby"]:
            stem = "/".join(parts[1:])
        else:
            continue
        for name in names(imported):
            submodule = files(f"{stem}/{name}" if stem else name)
            reached |= submodule or (files(stem) if stem else set())
    return reached


def _daemon_modules() -> dict[str, ast.Module]:
    """Parsed daemon modules that may spawn, following imports transitively into cli/ and install/.

    An import closure only bounds what the daemon can call, so _MUST_FORK names
    the CLI-only functions it reaches.
    """
    sources = {path.relative_to(_SRC).as_posix(): path.read_text() for path in _SRC.rglob("*.py")}
    pending = [relative for relative in sources if not relative.startswith(_NOT_DAEMON)]
    daemon = set(pending)
    while pending:
        relative = pending.pop()
        reached = _imported_modules(relative, sources[relative], sources.keys()) - daemon
        daemon |= reached
        pending.extend(reached)
    daemon.discard("utils/spawn.py")
    return {
        relative: ast.parse(sources[relative])
        for relative in daemon
        if _MAY_SPAWN.search(sources[relative])
    }


def test_daemon_spawns_go_through_the_helper() -> None:
    unexpected = []
    scanned = set()
    for relative, tree in sorted(_daemon_modules().items()):
        for qualname, call in _spawn_references(tree):
            scanned.add(f"{relative}::{qualname}")
            if f"{relative}::{qualname}" not in _MUST_FORK:
                unexpected.append(f"{relative}::{qualname} {call}")
    assert unexpected == []
    assert set(_MUST_FORK) - scanned == set()


@pytest.mark.parametrize(
    "source",
    [
        "async def f(loop, protocol):\n    await loop.subprocess_exec(protocol, 'ls')\n",
        "from asyncio import subprocess\nasync def f():\n    await subprocess.create_subprocess_exec('ls')\n",
        "import asyncio.subprocess\nasync def f():\n    await asyncio.subprocess.create_subprocess_shell('ls')\n",
        "from concurrent.futures import ProcessPoolExecutor\ndef f():\n    return ProcessPoolExecutor()\n",
        "import multiprocessing as mp\ndef f():\n    mp.Process(target=print).start()\n",
        "import os\ndef f():\n    os.spawnv(os.P_WAIT, '/bin/ls', ['ls'])\n",
    ],
)
def test_the_scan_sees_each_process_creating_form(source: str) -> None:
    assert _MAY_SPAWN.search(source)
    assert [qualname for qualname, _ in _spawn_references(ast.parse(source))] == ["f"]


def test_helper_builds_posix_spawn_eligible_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def record(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return None

    async def record_async(*args: Any, **kwargs: Any) -> Any:
        return record(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", record)
    monkeypatch.setattr(subprocess, "Popen", record)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", record_async)
    spawn.run(["git", "status"], capture_output=True)
    spawn.popen(["git", "status"], text=True)
    asyncio.run(spawn.create_subprocess_exec("git", "status", cwd=tmp_path))

    (run_args, run_kwargs), (popen_args, popen_kwargs), (exec_args, exec_kwargs) = calls
    # argv stays as written; CPython checks the executable path, not argv[0].
    assert run_args == popen_args == (["git", "status"],)
    assert Path(run_kwargs["executable"]).is_absolute()
    assert popen_kwargs["executable"] == run_kwargs["executable"]
    assert exec_args[:5] == ("/bin/sh", "-c", spawn._CHDIR_EXEC, "gobby-spawn", str(tmp_path))
    assert exec_args[5] == run_kwargs["executable"] and exec_args[6:] == ("status",)
    assert exec_kwargs["executable"] == "/bin/sh"
    for kwargs in (run_kwargs, popen_kwargs, exec_kwargs):
        assert kwargs["close_fds"] is False and kwargs["cwd"] is None
        assert not {"start_new_session", "process_group", "pass_fds"} & kwargs.keys()
    with pytest.raises(TypeError):
        spawn.run(["git", "status"], start_new_session=True)
    with pytest.raises(FileNotFoundError):
        spawn.run(["git", "status"], cwd=tmp_path / "missing")
    # A command that is not installed reaches subprocess as written, which raises its own error.
    calls.clear()
    spawn.run(["gobby-no-such-command-22815", "x"], cwd=tmp_path)
    assert calls == [
        (
            (["gobby-no-such-command-22815", "x"],),
            {
                "executable": None,
                "cwd": str(tmp_path),
                "env": None,
                "close_fds": False,
                "text": False,
            },
        )
    ]
    monkeypatch.undo()
    with pytest.raises(FileNotFoundError):
        spawn.run(["gobby-no-such-command-22815"])


def test_helper_runs_in_the_directory_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "work").mkdir()
    (tmp_path / "decoy" / "work").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    # A shell cd searches CDPATH for a relative directory; Popen's cwd never does.
    env = {**os.environ, "CDPATH": str(tmp_path / "decoy")}

    result = spawn.run(["pwd", "-P"], cwd="work", env=env, capture_output=True, text=True)

    assert result.stdout == f"{(tmp_path / 'work').resolve()}\n"


def test_an_absolute_directory_needs_no_daemon_cwd(tmp_path: Path) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    previous = os.getcwd()
    os.chdir(gone)
    try:
        gone.rmdir()
        result = spawn.run(["pwd", "-P"], cwd=tmp_path, capture_output=True, text=True)
    finally:
        os.chdir(previous)

    assert result.stdout == f"{tmp_path.resolve()}\n"


@pytest.mark.skipif(sys.platform != "darwin", reason="posix_spawn eligibility is macOS-specific")
def test_helper_never_takes_the_fork_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("fork_exec forks the whole daemon")

    monkeypatch.setattr(subprocess, "_fork_exec", forbidden)
    assert spawn.run(["pwd"], cwd=tmp_path, capture_output=True, text=True).stdout.strip() == str(
        tmp_path.resolve()
    )

    async def exec_from(directory: Path) -> bytes:
        process = await spawn.create_subprocess_exec(
            "pwd", cwd=directory, stdout=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        return stdout

    assert asyncio.run(exec_from(tmp_path)).decode().strip() == str(tmp_path.resolve())


@pytest.mark.skipif(os.name != "posix", reason="POSIX sessions")
def test_a_session_leader_starts_without_forking(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("fork_exec forks the whole daemon")

    spawned: list[dict[str, Any]] = []
    real_posix_spawn = os.posix_spawn

    def recording_posix_spawn(path: str, argv: Any, env: Any, **kwargs: Any) -> int:
        spawned.append(kwargs)
        return real_posix_spawn(path, argv, env, **kwargs)

    monkeypatch.setattr(subprocess, "_fork_exec", forbidden)
    monkeypatch.setattr(os, "posix_spawn", recording_posix_spawn)
    program = "import os,sys\nprint(os.getsid(0))\nprint('late', file=sys.stderr)\n"

    async def lead_a_session() -> tuple[int, tuple[bytes, bytes], int]:
        process = await spawn.create_session_exec(sys.executable, "-c", program)
        # Both awaiters share the one reap; a second waitpid would fail with ECHILD.
        output, returncode = await asyncio.gather(process.communicate(), process.wait())
        return process.pid, output, returncode

    pid, (stdout, stderr), returncode = asyncio.run(lead_a_session())

    assert (int(stdout), stderr, returncode) == (pid, b"late\n", 0)
    assert [kwargs["setsid"] for kwargs in spawned] == [True]
