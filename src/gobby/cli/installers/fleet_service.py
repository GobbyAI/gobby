"""Supervise the independent native fleet sampler."""

import json
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from uuid import UUID

from gobby.fleet_health.state import fleet_state_path

from . import service_common

_LABEL = "com.gobby.fleet-watch"
_UNIT = "gobby-fleet-watch.service"


def _service_path() -> Path | None:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"
    if platform.system() == "Linux":
        return Path.home() / ".config" / "systemd" / "user" / _UNIT
    return None


def _run(command: list[str], *, required: bool = True) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if required and result.returncode:
        raise RuntimeError(f"Fleet watcher service failed: {result.stderr or result.stdout}")
    return result.stdout or ""


def _supervised_pid() -> int | None:
    if platform.system() == "Darwin":
        output = _run(["launchctl", "print", f"gui/{os.getuid()}/{_LABEL}"], required=False)
        match = re.search(r"\bpid\s*=\s*(\d+)", output)
        return int(match[1]) if match else None
    output = _run(
        ["systemctl", "--user", "show", _UNIT, "--property=MainPID", "--value"],
        required=False,
    ).strip()
    return int(output) if output.isdecimal() else None


def _sample_identity(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def _load_and_wait(path: Path) -> None:
    state = fleet_state_path()
    previous = _sample_identity(state)
    started = time.time()
    if platform.system() == "Darwin":
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootout", f"{domain}/{_LABEL}"], required=False)
        _run(["launchctl", "enable", f"{domain}/{_LABEL}"])
        _run(["launchctl", "bootstrap", domain, str(path)])
    else:
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", _UNIT])
        _run(["systemctl", "--user", "restart", _UNIT])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            sample = json.loads(state.read_text())
            stamp = sample.get("sampled_at_unix")
            pid = sample.get("watcher_pid")
            if (
                _sample_identity(state) != previous
                and isinstance(stamp, int)
                and int(started) <= stamp <= time.time()
                and time.time() - stamp < 30
                and isinstance(pid, int)
                and pid > 0
                and pid == _supervised_pid()
            ):
                return
        except (OSError, ValueError, AttributeError):
            pass
        time.sleep(0.1)
    raise RuntimeError("Fleet watcher did not publish a fresh sample within 60 seconds")


def install_fleet_service(project_path: Path) -> bool:
    """Install the platform service and await its first sample."""
    path = _service_path()
    if path is None:
        return False
    context = service_common._resolve_install_context()
    config = project_path / ".gobby" / "project.json"
    project_id = str(UUID(json.loads(config.read_text())["id"])) if config.exists() else ""
    context["fleet_project_id"] = project_id
    logs = Path(str(context["runtime_log_file"])).parent
    logs.mkdir(parents=True, exist_ok=True)
    context["fleet_logs_dir"] = str(logs)
    context["fleet_log_file"] = str(logs / "fleet-watch.log")
    template = (
        "com.gobby.fleet-watch.plist.j2"
        if platform.system() == "Darwin"
        else "gobby-fleet-watch.service.j2"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(service_common._render_template(template, **context), encoding="utf-8")
    _load_and_wait(path)
    return True


def reload_fleet_service() -> None:
    """Reload an installed watcher after native binary promotion."""
    path = _service_path()
    if path is not None and path.exists():
        _load_and_wait(path)
