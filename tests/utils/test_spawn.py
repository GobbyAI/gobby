"""Daemon spawns start through posix_spawn (#22815)."""

from __future__ import annotations

import ast
import asyncio
import os
import re
import subprocess
import sys
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
        "os.system",
        "os.popen",
        "os.fork",
        "os.forkpty",
        "pty.spawn",
        "pty.fork",
    }
)
# Every call in _SPAWN_CALLS names its module or function in the source text, so
# the scan parses only the files that could hold one.
_MAY_SPAWN = re.compile(r"subprocess|\bsystem\b|\bpopen\b|\bfork(?:pty)?\b|\bpty\b")
# CLI commands, processes of their own (the stdio MCP proxy, Chrome's supervisor)
# and bundled templates. Daemon code also calls into some of these modules, so
# every module a daemon module imports is scanned as well.
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
# daemon sites that need fork-only options, and the CLI-only functions of modules
# the daemon imports.
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
    "ai/_text_generation_adapters.py::_run_cli_text_generation_command": (
        "own session: a timeout killpg()s the CLI's children; once per generation, off the hook path"
    ),
    "ai/_text_generation_adapters.py::DroidCLITextGenerateAdapter.generate": (
        "own session: a timeout killpg()s droid's children; once per generation"
    ),
    "code_index/gcode_gateway.py::GcodeGateway._run_command": (
        "own session: a timeout killpg()s gcode's children; absent from the hook-path capture"
    ),
    "code_index/gcode_gateway.py::GcodeGateway._run_command_result": (
        "own session: a timeout killpg()s gcode's children; absent from the hook-path capture"
    ),
    "mcp_proxy/tools/merge_landscape.py::register_merge_landscape_tools.verify_in_worktree": (
        "own session: a timeout killpg()s the verify command's children; once per merge check"
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
        "own session: detach killpg()s agy's children; once per chat attach"
    ),
    "servers/websocket/chat/backends/droid.py::DroidWebChatBackend.attach_session": (
        "own session: detach killpg()s droid's children; once per chat attach"
    ),
    "skills/materialization.py::_run_owned_subprocess": (
        "own session and pass_fds for the ownership pipe; once per skill materialization"
    ),
    "terminals/host_manager.py::TerminalHostManager._spawn_host_process": (
        "own session: the gterm host outlives daemon restarts; once per host start"
    ),
    "utils/daemon_git.py::_spawn_git": (
        "the Popen session fallback only runs where os.posix_spawn is missing"
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
        if isinstance(node, ast.ImportFrom) and node.module in {
            "subprocess",
            "os",
            "pty",
            "asyncio",
        }:
            for name in node.names:
                aliases[name.asname or name.name] = f"{node.module}.{name.name}"
        elif isinstance(node, ast.Import):
            for name in node.names:
                if name.name in {"subprocess", "os", "pty", "asyncio"} and name.asname:
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
                if resolved in _SPAWN_CALLS or resolved in {"subprocess", "pty"}:
                    found.append((".".join(scope) or "<module>", resolved))
            visit(child, scope)

    visit(tree, [])
    return found


def _daemon_sources() -> dict[str, str]:
    """Source of every daemon module and every module a daemon module imports."""
    sources = {path.relative_to(_SRC).as_posix(): path.read_text() for path in _SRC.rglob("*.py")}
    daemon = {relative for relative in sources if not relative.startswith(_NOT_DAEMON)}
    for relative in list(daemon):
        for module in re.findall(r"^\s*(?:from|import)\s+gobby\.([\w.]+)", sources[relative], re.M):
            stem = module.replace(".", "/")
            daemon.update({f"{stem}.py", f"{stem}/__init__.py"} & sources.keys())
    daemon.discard("utils/spawn.py")
    return {relative: sources[relative] for relative in sorted(daemon)}


def test_daemon_spawns_go_through_the_helper() -> None:
    unexpected = []
    for relative, source in _daemon_sources().items():
        if not _MAY_SPAWN.search(source):
            continue
        for qualname, call in _spawn_references(ast.parse(source)):
            if f"{relative}::{qualname}" not in _MUST_FORK:
                unexpected.append(f"{relative}::{qualname} {call}")
    assert unexpected == []


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
