"""Executable Guard set G: vt-engine coverage and session-owned host checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import psutil

REQUIRED_GATED_TARGETS = frozenset(
    {
        "embed",
        "host_lifecycle",
        "control_protocol",
        "frame_protocol",
        "frame_producer",
    }
)
INSTALLED_BINARIES = ("gterm", "gcode", "gdaemon", "ghook")
HOST_WRAP_GROUPS = frozenset({2, 3})
_RUN_ROOT_ENV = "GOBBY_GUARD_SET_G_RUN_ROOT"
_ISOLATED_ENV_KEYS = (_RUN_ROOT_ENV, "CLAUDE_CODE_TMPDIR")
_UNIX_SOCKET_MAX = 104
_SOCKET_NEST = Path(".tmpxxxxxx") / "gterm-control.sock"
_RUN_ROOT_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"

CommandRunner = Callable[[Sequence[str], Mapping[str, str]], int]
HostSnapshot = Callable[[], dict[int, Path]]
FinalizeHost = Callable[[int, Path, tuple[Path, ...]], None]


class GuardSetGError(RuntimeError):
    """Guard set G failed a coverage or host-ownership check."""


@dataclass
class GuardOutcome:
    """Result of wrapping a command or scanning for leaked hosts."""

    command_returncode: int = 0
    leak_recorded: bool = False
    remaining: dict[int, Path] = field(default_factory=dict)
    ended_durable: dict[int, Path] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        if self.leak_recorded or self.remaining or self.ended_durable:
            return self.command_returncode or 1
        return self.command_returncode


def socket_dir_from_cmdline(cmdline: Sequence[str] | None) -> Path | None:
    if cmdline is None or len(cmdline) < 2:
        return None
    if Path(cmdline[0]).name != "gterm" or cmdline[1] != "host":
        return None
    try:
        socket_index = list(cmdline).index("--socket-dir") + 1
        return Path(cmdline[socket_index]).resolve()
    except (ValueError, IndexError):
        return None


def snapshot_gterm_hosts(
    processes: Sequence[tuple[int, Sequence[str]]] | None = None,
) -> dict[int, Path]:
    if processes is not None:
        hosts: dict[int, Path] = {}
        for pid, cmdline in processes:
            socket_dir = socket_dir_from_cmdline(cmdline)
            if socket_dir is not None:
                hosts[int(pid)] = socket_dir
        return hosts
    hosts = {}
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            socket_dir = socket_dir_from_cmdline(process.info.get("cmdline") or [])
            if socket_dir is not None:
                hosts[int(process.info["pid"])] = socket_dir
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return hosts


def path_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def leaked_hosts(
    before: Mapping[int, Path],
    after: Mapping[int, Path],
    roots: tuple[Path, ...],
) -> dict[int, Path]:
    return {
        pid: socket_dir
        for pid, socket_dir in after.items()
        if before.get(pid) != socket_dir and any(path_under(socket_dir, root) for root in roots)
    }


def durable_hosts(
    hosts: Mapping[int, Path],
    temp_roots: tuple[Path, ...],
) -> dict[int, Path]:
    return {
        pid: socket_dir
        for pid, socket_dir in hosts.items()
        if not any(path_under(socket_dir, root) for root in temp_roots)
    }


def ended_durable_hosts(
    before: Mapping[int, Path],
    after: Mapping[int, Path],
    temp_roots: tuple[Path, ...],
) -> dict[int, Path]:
    return {
        pid: socket_dir
        for pid, socket_dir in durable_hosts(before, temp_roots).items()
        if after.get(pid) != socket_dir
    }


def isolated_run_roots(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    values = os.environ if env is None else env
    roots: list[Path] = []
    seen: set[Path] = set()
    for key in _ISOLATED_ENV_KEYS:
        raw = values.get(key, "").strip()
        if not raw:
            continue
        root = Path(raw).resolve()
        if root in seen:
            continue
        seen.add(root)
        roots.append(root)
    return tuple(roots)


def default_temp_roots(run_roots: tuple[Path, ...]) -> tuple[Path, ...]:
    extras = (Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve())
    ordered: list[Path] = []
    seen: set[Path] = set()
    for root in (*run_roots, *extras):
        if root in seen:
            continue
        seen.add(root)
        ordered.append(root)
    return tuple(ordered)


def finalize_pidfile_host(pid: int, socket_dir: Path, roots: tuple[Path, ...]) -> None:
    if not any(path_under(socket_dir, root) for root in roots):
        return
    try:
        recorded_pid = int((socket_dir / "gterm.pid").read_text().strip())
    except (OSError, ValueError):
        return
    if recorded_pid != pid:
        return
    try:
        process = psutil.Process(pid)
        if socket_dir_from_cmdline(process.cmdline()) != socket_dir:
            return
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.0)
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return


def _emit(message: str, stream: TextIO | None = None) -> None:
    print(message, file=stream or sys.stderr)


def _classify(socket_dir: Path, run_roots: tuple[Path, ...]) -> str:
    if any(path_under(socket_dir, root) for root in run_roots):
        return "owned"
    return "durable"


def _report_hosts(hosts: Mapping[int, Path], run_roots: tuple[Path, ...]) -> None:
    if not hosts:
        _emit("gterm hosts: none")
        return
    for pid, socket_dir in sorted(hosts.items()):
        classification = _classify(socket_dir, run_roots)
        _emit(f"pid={pid} socket_dir={socket_dir} classification={classification}")


def _after_check(
    *,
    before: Mapping[int, Path],
    command_returncode: int,
    run_roots: tuple[Path, ...],
    temp_roots: tuple[Path, ...],
    snapshot: HostSnapshot,
    finalize: FinalizeHost,
) -> GuardOutcome:
    after = snapshot()
    leaked = leaked_hosts(before, after, run_roots)
    leak_recorded = bool(leaked)
    if leaked:
        _emit("session-owned gterm host leak:")
        _report_hosts(leaked, run_roots)
    for pid, socket_dir in leaked.items():
        finalize(pid, socket_dir, run_roots)
    final = snapshot()
    remaining = leaked_hosts(before, final, run_roots)
    ended = ended_durable_hosts(before, final, temp_roots)
    if remaining:
        _emit("surviving session-owned gterm hosts:")
        _report_hosts(remaining, run_roots)
    if ended:
        _emit("ended durable gterm hosts:")
        _report_hosts(ended, run_roots)
    _report_hosts(final, run_roots)
    return GuardOutcome(
        command_returncode=command_returncode,
        leak_recorded=leak_recorded,
        remaining=remaining,
        ended_durable=ended,
    )


def wrap_command(
    argv: Sequence[str],
    *,
    run_roots: tuple[Path, ...],
    runner: CommandRunner | None = None,
    snapshot: HostSnapshot | None = None,
    finalize: FinalizeHost | None = None,
    env: Mapping[str, str] | None = None,
    temp_roots: tuple[Path, ...] | None = None,
) -> GuardOutcome:
    take_snapshot = snapshot or snapshot_gterm_hosts
    do_finalize = finalize or finalize_pidfile_host
    run = runner or _default_runner
    roots = temp_roots if temp_roots is not None else default_temp_roots(run_roots)
    before = take_snapshot()
    command_returncode = 0
    outcome = GuardOutcome(command_returncode=1)
    try:
        command_returncode = run(list(argv), dict(env or {}))
    except Exception as exc:
        _emit(f"guard-set-g command raised: {exc}")
        command_returncode = 1
    finally:
        outcome = _after_check(
            before=before,
            command_returncode=command_returncode,
            run_roots=run_roots,
            temp_roots=roots,
            snapshot=take_snapshot,
            finalize=do_finalize,
        )
    return outcome


def run_wrapped(
    body: Callable[[], int],
    *,
    run_roots: tuple[Path, ...],
    snapshot: HostSnapshot | None = None,
    finalize: FinalizeHost | None = None,
    temp_roots: tuple[Path, ...] | None = None,
) -> GuardOutcome:
    take_snapshot = snapshot or snapshot_gterm_hosts
    do_finalize = finalize or finalize_pidfile_host
    roots = temp_roots if temp_roots is not None else default_temp_roots(run_roots)
    before = take_snapshot()
    command_returncode = 0
    outcome = GuardOutcome(command_returncode=1)
    try:
        command_returncode = body()
    except Exception as exc:
        _emit(f"guard-set-g command raised: {exc}")
        command_returncode = 1
    finally:
        outcome = _after_check(
            before=before,
            command_returncode=command_returncode,
            run_roots=run_roots,
            temp_roots=roots,
            snapshot=take_snapshot,
            finalize=do_finalize,
        )
    return outcome


def check_hosts(
    *,
    run_roots: tuple[Path, ...],
    snapshot: HostSnapshot | None = None,
    finalize: FinalizeHost | None = None,
    temp_roots: tuple[Path, ...] | None = None,
) -> GuardOutcome:
    take_snapshot = snapshot or snapshot_gterm_hosts
    do_finalize = finalize or finalize_pidfile_host
    roots = temp_roots if temp_roots is not None else default_temp_roots(run_roots)
    current = take_snapshot()
    owned = {
        pid: socket_dir
        for pid, socket_dir in current.items()
        if any(path_under(socket_dir, root) for root in run_roots)
    }
    _report_hosts(current, run_roots)
    leak_recorded = bool(owned)
    for pid, socket_dir in owned.items():
        do_finalize(pid, socket_dir, run_roots)
    final = take_snapshot()
    remaining = {
        pid: socket_dir
        for pid, socket_dir in final.items()
        if any(path_under(socket_dir, root) for root in run_roots)
    }
    ended = ended_durable_hosts(current, final, roots)
    if remaining:
        _emit("surviving session-owned gterm hosts:")
        _report_hosts(remaining, run_roots)
    return GuardOutcome(
        command_returncode=0,
        leak_recorded=leak_recorded,
        remaining=remaining,
        ended_durable=ended,
    )


def terminal_clippy_argv() -> list[str]:
    return [
        "cargo",
        "clippy",
        "-p",
        "gobby-terminal",
        "--all-targets",
        "--features",
        "vt-engine",
        "--",
        "-D",
        "warnings",
    ]


def terminal_nextest_list_argv() -> list[str]:
    return [
        "cargo",
        "nextest",
        "list",
        "-p",
        "gobby-terminal",
        "--features",
        "vt-engine",
        "--message-format",
        "json",
        "--cargo-quiet",
    ]


def terminal_nextest_run_argv() -> list[str]:
    return [
        "cargo",
        "nextest",
        "run",
        "-p",
        "gobby-terminal",
        "--features",
        "vt-engine",
        "--no-tests",
        "fail",
        "--final-status-level",
        "skip",
    ]


def client_clippy_argv() -> list[str]:
    return [
        "cargo",
        "clippy",
        "-p",
        "gobby-client",
        "--all-targets",
        "--",
        "-D",
        "warnings",
    ]


def client_nextest_argv() -> list[str]:
    return [
        "cargo",
        "nextest",
        "run",
        "-p",
        "gobby-client",
        "--no-tests",
        "fail",
    ]


def _suites(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rust_suites = payload.get("rust-suites")
    if not isinstance(rust_suites, dict):
        return []
    return [suite for suite in rust_suites.values() if isinstance(suite, dict)]


def _binary_name(suite: Mapping[str, Any]) -> str:
    name = suite.get("binary-name")
    if isinstance(name, str) and name:
        return name
    binary_id = suite.get("binary-id")
    if isinstance(binary_id, str) and "::" in binary_id:
        return binary_id.rsplit("::", 1)[-1]
    return ""


def _target_executed(suite: Mapping[str, Any]) -> bool:
    testcases = suite.get("testcases")
    if not isinstance(testcases, dict) or not testcases:
        return False
    for case in testcases.values():
        if not isinstance(case, dict):
            return True
        if case.get("skip") is True:
            continue
        status = str(case.get("status", "")).lower()
        if status in {"skipped", "skip"}:
            continue
        return True
    return False


def evaluate_gated_targets(payload: Mapping[str, Any] | str) -> frozenset[str]:
    parsed: Mapping[str, Any]
    if isinstance(payload, str):
        start = payload.find("{")
        end = payload.rfind("}")
        if start == -1 or end < start:
            raise GuardSetGError("nextest list JSON must be an object")
        loaded = json.loads(payload[start : end + 1])
        if not isinstance(loaded, dict):
            raise GuardSetGError("nextest list JSON must be an object")
        parsed = loaded
    else:
        parsed = payload
    by_name = {_binary_name(suite): suite for suite in _suites(parsed)}
    missing: list[str] = []
    empty: list[str] = []
    skipped: list[str] = []
    for name in sorted(REQUIRED_GATED_TARGETS):
        suite = by_name.get(name)
        if suite is None:
            missing.append(name)
            continue
        testcases = suite.get("testcases")
        if not isinstance(testcases, dict) or not testcases:
            empty.append(name)
            continue
        if not _target_executed(suite):
            skipped.append(name)
    problems: list[str] = []
    if missing:
        problems.append(f"missing required gated targets: {', '.join(missing)}")
    if empty:
        problems.append(f"required gated targets executed zero tests: {', '.join(empty)}")
    if skipped:
        problems.append(f"required gated targets skipped: {', '.join(skipped)}")
    if problems:
        raise GuardSetGError("; ".join(problems))
    return frozenset(REQUIRED_GATED_TARGETS)


def installed_bin_dir() -> Path:
    return Path.home() / ".gobby" / "bin"


def binary_provenance(name: str, bin_dir: Path | None = None) -> str:
    path = (bin_dir or installed_bin_dir()) / name
    if not path.is_file():
        return f"native-binary name={name} source=installed path={path} missing"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    stat = path.stat()
    return (
        f"native-binary name={name} source=installed path={path} "
        f"sha256={digest} device={stat.st_dev} inode={stat.st_ino}"
    )


def print_installed_provenance(bin_dir: Path | None = None) -> None:
    root = bin_dir or installed_bin_dir()
    _emit(f"installed-bin-dir {root}")
    for name in INSTALLED_BINARIES:
        _emit(binary_provenance(name, root))


def _default_runner(argv: Sequence[str], env: Mapping[str, str]) -> int:
    merged = os.environ.copy()
    merged.update(env)
    completed = subprocess.run(list(argv), env=merged, check=False)
    return int(completed.returncode)


def _run_captured(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
    merged = os.environ.copy()
    merged.update(env)
    completed = subprocess.run(
        list(argv),
        env=merged,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return int(completed.returncode), completed.stdout


GROUP_1_PYTEST = (
    "tests/test_runner_lifecycle_restart_replay.py",
    "tests/agents/test_resume_executor.py",
    "tests/agents/test_spawn_executor.py",
    "tests/agents/test_tmux.py",
    "tests/agents/test_lifecycle_monitor.py",
    "tests/agents/test_capture_consumers.py",
    "tests/config/test_runtime_config_contract.py",
    "tests/config/test_terminal_config.py",
    "tests/cli/test_install_setup_gterm.py",
    "tests/gterminal/test_vendor_layer.py",
    "tests/mcp_proxy/tools/sessions/test_terminal.py",
    "tests/mcp_proxy/tools/sessions/test_terminal_clear.py",
    "tests/servers/test_tmux_mixin.py",
    "tests/servers/test_admin_health.py",
    "tests/install/test_version_pins.py",
    "tests/install/test_distribution.py",
    "tests/tasks/test_validation_evidence.py",
)

GROUP_2_PYTEST = (
    "tests/terminals",
    "tests/storage/test_terminals.py",
    "tests/servers/test_terminal_ws_create.py",
    "tests/servers/test_terminal_ws_golden.py",
    "tests/servers/test_terminal_ws_lease.py",
    "tests/servers/test_terminal_ws_rename.py",
    "tests/servers/test_terminal_ws_viewport.py",
    "tests/servers/test_tmux_bridge_authority.py",
    "tests/servers/test_native_web_proxy.py",
    "tests/servers/test_attention_respond.py",
    "tests/mcp_proxy/test_sessions_terminal_tools.py",
)


def _pytest_argv(paths: Sequence[str]) -> list[str]:
    return ["uv", "run", "pytest", *paths]


def gterm_socket_path_budget(run_root: Path) -> int:
    sample = run_root.resolve() / _SOCKET_NEST
    return len(os.fsencode(str(sample)))


def isolated_run_root_name_budget(parent: Path) -> int:
    """Max unique directory name length under parent that still fits sockaddr_un."""
    return _UNIX_SOCKET_MAX - gterm_socket_path_budget(parent / "x")


def _parent_is_writable(candidate: Path) -> bool:
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    for suffix in ("", "x"):
        probe = candidate / f".gw{os.getpid()}{suffix}"
        try:
            probe.mkdir()
        except FileExistsError:
            continue
        except OSError:
            return False
        try:
            probe.rmdir()
        except OSError:
            return True
        return True
    return False


def isolated_temp_parent() -> Path:
    """Pick a writable parent so nested gterm Unix sockets fit sockaddr_un."""
    raw_candidates = (
        os.environ.get("CLAUDE_CODE_TMPDIR", ""),
        os.environ.get("TMPDIR", ""),
        tempfile.gettempdir(),
        "/tmp",
        "/private/tmp",
        str(Path.home() / ".gobby" / "tmp"),
    )
    seen: set[Path] = set()
    for raw in raw_candidates:
        if not str(raw).strip():
            continue
        candidate = Path(raw)
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if not _parent_is_writable(candidate):
            continue
        if gterm_socket_path_budget(resolved) < _UNIX_SOCKET_MAX:
            return candidate
        if isolated_run_root_name_budget(resolved) >= 1:
            return candidate
    raise GuardSetGError("no writable temp parent keeps gterm sockets under the sockaddr_un limit")


def _mkdir_unique(parent: Path, name_len: int) -> Path:
    for _ in range(128):
        raw = os.urandom(name_len)
        name = "".join(_RUN_ROOT_CHARS[byte % 36] for byte in raw)
        path = parent / name
        try:
            path.mkdir()
        except FileExistsError:
            continue
        except OSError as exc:
            raise GuardSetGError(f"could not create isolated run root under {parent}") from exc
        return path.resolve()
    raise GuardSetGError(f"could not allocate a unique isolated run root under {parent}")


@contextmanager
def isolated_run_root(parent: Path | None = None) -> Iterator[Path]:
    root_parent = parent if parent is not None else isolated_temp_parent()
    name_budget = isolated_run_root_name_budget(root_parent)
    owned: Path | None = None
    # Prefer the writable parent itself. A nested unique dir becomes TMPDIR and
    # breaks vendored zig helpers that spawn from relative cache paths.
    if gterm_socket_path_budget(root_parent) < _UNIX_SOCKET_MAX:
        run_root = root_parent.resolve()
    elif name_budget >= 1:
        owned = _mkdir_unique(root_parent, min(name_budget, 12))
        run_root = owned
    else:
        raise GuardSetGError(
            f"isolated temp parent {root_parent} socket path budget "
            f"{gterm_socket_path_budget(root_parent)} exceeds {_UNIX_SOCKET_MAX}"
        )
    try:
        budget = gterm_socket_path_budget(run_root)
        if budget >= _UNIX_SOCKET_MAX:
            raise GuardSetGError(
                f"isolated run root {run_root} socket path budget {budget} "
                f"exceeds {_UNIX_SOCKET_MAX}"
            )
        yield run_root
    finally:
        if owned is not None:
            shutil.rmtree(owned, ignore_errors=True)


def _seed_sandbox_zig_packages(env: Mapping[str, str]) -> None:
    """Expose the machine Zig package cache inside sandbox/global Zig caches."""
    source = Path.home() / ".cache" / "zig" / "p"
    if not source.is_dir():
        return
    destinations: list[Path] = []
    xdg = env.get("XDG_CACHE_HOME", "").strip()
    if xdg:
        destinations.append(Path(xdg) / "zig" / "p")
    zig_global = env.get("ZIG_GLOBAL_CACHE_DIR", "").strip()
    if zig_global:
        destinations.append(Path(zig_global) / "p")
    for dest in destinations:
        try:
            dest.mkdir(parents=True, exist_ok=True)
            for entry in source.iterdir():
                target = dest / entry.name
                if target.exists() or target.is_symlink():
                    continue
                target.symlink_to(entry)
        except OSError as exc:
            _emit(f"zig package seed skipped: {exc}")


def _isolated_child_env(
    run_root: Path,
    base: Mapping[str, str] | None = None,
    *,
    repo: Path | None = None,
) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    root = str(run_root)
    env[_RUN_ROOT_ENV] = root
    env["TMPDIR"] = root
    env["TMP"] = root
    env["TEMP"] = root
    env["CLAUDE_CODE_TMPDIR"] = root
    repo_root = repo if repo is not None else Path.cwd()
    vendor_cache = repo_root / "crates" / "gterminal" / "vendor" / "libghostty-vt" / ".zig-cache"
    if not env.get("ZIG_GLOBAL_CACHE_DIR", "").strip():
        try:
            vendor_cache.mkdir(parents=True, exist_ok=True)
            env["ZIG_GLOBAL_CACHE_DIR"] = str(vendor_cache)
        except OSError as exc:
            _emit(f"zig global cache mkdir skipped: {exc}")
    machine_pkgs = Path.home() / ".cache" / "zig" / "p"
    if not env.get("LIBGHOSTTY_VT_ZIG_SYSTEM_DIR", "").strip() and machine_pkgs.is_dir():
        env["LIBGHOSTTY_VT_ZIG_SYSTEM_DIR"] = str(machine_pkgs)
    _seed_sandbox_zig_packages(env)
    return env


def _run_group_3_body(env: Mapping[str, str]) -> int:
    code = _default_runner(terminal_clippy_argv(), env)
    if code:
        return code
    list_code, list_out = _run_captured(terminal_nextest_list_argv(), env)
    if list_code:
        return list_code
    try:
        evaluate_gated_targets(list_out)
    except GuardSetGError as exc:
        _emit(str(exc))
        return 1
    code = _default_runner(terminal_nextest_run_argv(), env)
    if code:
        return code
    code = _default_runner(client_clippy_argv(), env)
    if code:
        return code
    return _default_runner(client_nextest_argv(), env)


def _run_group_body(group: int, env: Mapping[str, str], repo: Path) -> int:
    if group == 1:
        return _default_runner(_pytest_argv(GROUP_1_PYTEST), env)
    if group == 2:
        return _default_runner(_pytest_argv(GROUP_2_PYTEST), env)
    if group == 3:
        return _run_group_3_body(env)
    if group == 4:
        return _default_runner(
            ["cargo", "nextest", "run", "-p", "gobby-core", "-p", "gobby-daemon"],
            env,
        )
    if group == 5:
        for argv in (
            ["uv", "run", "ruff", "check", "src/"],
            ["uv", "run", "ruff", "format", "--check", "src/"],
            ["uv", "run", "mypy", "src/"],
            [
                "uv",
                "run",
                "gobby",
                "test-types",
                "audit",
                "tests/",
                "--baseline",
                ".gobby/test-types-baseline.json",
                "--fail-on-new",
            ],
        ):
            code = _default_runner(argv, env)
            if code:
                return code
        return 0
    if group == 6:
        completed = subprocess.run(
            [
                "npx",
                "--no-install",
                "vitest",
                "run",
                "hooks/",
                "activitySessionVisibility.test.ts",
            ],
            cwd=repo / "web",
            env={**os.environ, **dict(env)},
            check=False,
        )
        return int(completed.returncode)
    raise GuardSetGError(f"unsupported group {group}")


def run_group(group: int, *, repo: Path | None = None) -> int:
    root = repo if repo is not None else Path.cwd()
    _emit(f"guard-set-g group {group}")
    print_installed_provenance()
    if group == 7:
        outcome = check_hosts(run_roots=isolated_run_roots())
        return outcome.exit_code
    if group in HOST_WRAP_GROUPS:
        try:
            with isolated_run_root() as run_root:
                env = _isolated_child_env(run_root, repo=root)
                _emit(f"isolated run root {run_root}")
                outcome = run_wrapped(
                    lambda: _run_group_body(group, env, root),
                    run_roots=(run_root,),
                )
                return outcome.exit_code
        except GuardSetGError as exc:
            _emit(str(exc))
            return 1
    return _run_group_body(group, os.environ, root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gobby.guard_set_g",
        description="Run one Guard set G group with honest vt-engine and host checks.",
    )
    parser.add_argument("group", type=int, choices=range(1, 8), help="Guard set G group 1-7")
    parsed = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    return run_group(parsed.group)


if __name__ == "__main__":
    sys.exit(main())
