"""Machine-local intent and Tailscale Serve management for the web UI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - fixed tailscale CLI commands
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from gobby.config.bootstrap import UiExposureMode, load_bootstrap
from gobby.config.bootstrap_io import bootstrap_path, update_bootstrap_yaml

_READ_TIMEOUT_SECONDS = 5.0
_MUTATION_TIMEOUT_SECONDS = 15.0


class UiExposeError(RuntimeError):
    """Raised when UI exposure cannot be safely inspected or changed."""


@dataclass(frozen=True)
class UiExposeResult:
    """Current or resulting UI exposure state."""

    mode: UiExposureMode | None
    url: str | None = None
    changed: bool = False
    degraded_reason: str | None = None


@dataclass(frozen=True)
class _NodeInfo:
    dns_name: str

    @property
    def host_port(self) -> str:
        return f"{self.dns_name}:443"

    @property
    def url(self) -> str:
        return f"https://{self.dns_name}/"


@dataclass(frozen=True)
class _ServeState:
    node: _NodeInfo
    root_proxy: str | None


def enable_tailscale_ui(
    daemon_port: int,
    *,
    config_path: Path | None = None,
) -> UiExposeResult:
    """Enable and persist the root Tailscale Serve proxy for Gobby."""
    path = config_path or bootstrap_path()
    state = _probe_serve_state()
    target = _proxy_target(daemon_port)

    if state.root_proxy is not None:
        if not _is_gobby_proxy(state.root_proxy, daemon_port):
            raise UiExposeError("the Tailscale HTTPS root handler is owned by another proxy")
        try:
            _write_intent(path, "tailscale")
        except Exception as exc:
            raise UiExposeError(f"failed to persist UI exposure intent: {exc}") from exc
        return UiExposeResult(mode="tailscale", url=state.node.url)

    _run_mutation(
        [
            "serve",
            "--yes",
            "--bg",
            "--https=443",
            "--set-path=/",
            target,
        ]
    )
    try:
        verified = _probe_serve_state()
        if verified.root_proxy is None or not _is_gobby_proxy(verified.root_proxy, daemon_port):
            raise UiExposeError("Tailscale did not install the expected HTTPS root handler")
        _write_intent(path, "tailscale")
    except Exception as exc:
        _best_effort_remove_root(daemon_port)
        if isinstance(exc, UiExposeError):
            raise
        raise UiExposeError(f"failed to persist UI exposure intent: {exc}") from exc

    return UiExposeResult(mode="tailscale", url=verified.node.url, changed=True)


def disable_tailscale_ui(
    daemon_port: int,
    *,
    config_path: Path | None = None,
    forget: bool = False,
) -> UiExposeResult:
    """Disable the managed root handler, or only forget machine-local intent."""
    path = config_path or bootstrap_path()
    if forget:
        try:
            _write_intent(path, None)
        except Exception as exc:
            raise UiExposeError(f"failed to clear UI exposure intent: {exc}") from exc
        return UiExposeResult(mode=None)

    state = _probe_serve_state()
    if state.root_proxy is None:
        try:
            _write_intent(path, None)
        except Exception as exc:
            raise UiExposeError(f"failed to clear UI exposure intent: {exc}") from exc
        return UiExposeResult(mode=None)
    if not _is_gobby_proxy(state.root_proxy, daemon_port):
        raise UiExposeError("the Tailscale HTTPS root handler is owned by another proxy")

    _run_mutation(["serve", "--yes", "--https=443", "--set-path=/", "off"])
    try:
        verified = _probe_serve_state()
        if verified.root_proxy is not None:
            raise UiExposeError("Tailscale did not remove the HTTPS root handler")
        _write_intent(path, None)
    except Exception as exc:
        _best_effort_restore_root(daemon_port)
        if isinstance(exc, UiExposeError):
            raise
        raise UiExposeError(f"failed to clear UI exposure intent: {exc}") from exc

    return UiExposeResult(mode=None, changed=True)


def get_ui_exposure_status(
    daemon_port: int,
    *,
    config_path: Path | None = None,
) -> UiExposeResult:
    """Return configured exposure status without touching external state."""
    path = config_path or bootstrap_path()
    if _read_intent(path) is None:
        return UiExposeResult(mode=None)

    try:
        state = _probe_serve_state()
        if state.root_proxy is None:
            return UiExposeResult(
                mode="tailscale",
                degraded_reason="HTTPS root handler is missing",
            )
        if not _is_gobby_proxy(state.root_proxy, daemon_port):
            return UiExposeResult(
                mode="tailscale",
                degraded_reason="HTTPS root handler points to another proxy",
            )
        return UiExposeResult(mode="tailscale", url=state.node.url)
    except UiExposeError as exc:
        return UiExposeResult(mode="tailscale", degraded_reason=str(exc))


def reconcile_ui_exposure(
    daemon_port: int,
    *,
    config_path: Path | None = None,
) -> UiExposeResult | None:
    """Restore configured exposure after daemon readiness; skip absent intent."""
    path = config_path or bootstrap_path()
    if _read_intent(path) is None:
        return None
    return enable_tailscale_ui(daemon_port, config_path=path)


def resolve_installer_ui_exposure(
    explicit: bool | None,
    *,
    full_install: bool,
    no_interactive: bool,
    confirm: Callable[[], bool],
) -> bool:
    """Resolve installer consent without mutating Tailscale state."""
    if explicit is not None:
        return explicit
    if not full_install or no_interactive:
        return False
    try:
        _read_node_info()
    except UiExposeError:
        return False
    return confirm()


def apply_installer_ui_exposure(
    expose: bool,
    daemon_port: int,
    *,
    config_path: Path | None = None,
) -> UiExposeResult | None:
    """Apply an affirmative installer exposure choice."""
    if not expose:
        return None
    return enable_tailscale_ui(daemon_port, config_path=config_path)


def _read_intent(path: Path) -> UiExposureMode | None:
    return load_bootstrap(str(path)).ui_expose


def _write_intent(path: Path, mode: UiExposureMode | None) -> None:
    if mode is None and not path.exists():
        return
    if _read_intent(path) == mode:
        return

    def update(data: dict[str, Any]) -> None:
        if mode is None:
            data.pop("ui_expose", None)
        else:
            data["ui_expose"] = mode

    update_bootstrap_yaml(path, update)


def _probe_serve_state() -> _ServeState:
    node = _read_node_info()
    config = _run_json(["serve", "status", "--json"])
    tcp = _optional_mapping(config.get("TCP"), "TCP")
    web = _optional_mapping(config.get("Web"), "Web")
    allow_funnel = _optional_mapping(config.get("AllowFunnel"), "AllowFunnel")

    port_config = tcp.get("443")
    if port_config is not None:
        port_mapping = _required_mapping(port_config, 'TCP["443"]')
        if port_mapping.get("HTTPS") is not True or set(port_mapping) != {"HTTPS"}:
            raise UiExposeError("Tailscale port 443 has conflicting protocol state")

    funnel_value = allow_funnel.get(node.host_port)
    if funnel_value is not None and not isinstance(funnel_value, bool):
        raise UiExposeError("Tailscale AllowFunnel state is malformed")
    if funnel_value is True:
        raise UiExposeError("Tailscale Funnel is active for this node on port 443")

    host_config = web.get(node.host_port)
    if host_config is None:
        return _ServeState(node=node, root_proxy=None)
    host_mapping = _required_mapping(host_config, f'Web["{node.host_port}"]')
    handlers = _optional_mapping(host_mapping.get("Handlers"), "Handlers")
    root = handlers.get("/")
    if root is None:
        return _ServeState(node=node, root_proxy=None)
    if port_config is None:
        raise UiExposeError("Tailscale root handler exists without HTTPS port 443 state")
    root_mapping = _required_mapping(root, 'Handlers["/"]')
    if set(root_mapping) != {"Proxy"} or not isinstance(root_mapping.get("Proxy"), str):
        raise UiExposeError("the Tailscale HTTPS root handler is not a proxy")
    return _ServeState(node=node, root_proxy=cast(str, root_mapping["Proxy"]))


def _read_node_info() -> _NodeInfo:
    status = _run_json(["status", "--json"])
    if status.get("BackendState") != "Running":
        raise UiExposeError("Tailscale backend is not running")

    tailnet = _required_mapping(status.get("CurrentTailnet"), "CurrentTailnet")
    magic_dns_enabled = tailnet.get("MagicDNSEnabled")
    if magic_dns_enabled is not None and not isinstance(magic_dns_enabled, bool):
        raise UiExposeError("Tailscale MagicDNS state is malformed")
    if magic_dns_enabled is False:
        raise UiExposeError("Tailscale MagicDNS is disabled")

    self_status = _required_mapping(status.get("Self"), "Self")
    dns_name = self_status.get("DNSName")
    if not isinstance(dns_name, str) or not dns_name.strip("."):
        raise UiExposeError("Tailscale status is missing this node's MagicDNS name")
    return _NodeInfo(dns_name=dns_name.rstrip("."))


def _run_json(arguments: list[str]) -> dict[str, object]:
    executable = shutil.which("tailscale")
    if executable is None:
        raise UiExposeError("tailscale executable was not found")
    try:
        result = subprocess.run(  # nosec B603 - executable resolved from PATH, fixed arguments
            [executable, *arguments],
            capture_output=True,
            text=True,
            timeout=_READ_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UiExposeError("tailscale command timed out") from exc
    except OSError as exc:
        raise UiExposeError(f"tailscale command failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise UiExposeError(f"tailscale command failed: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise UiExposeError("tailscale returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise UiExposeError("tailscale returned malformed JSON")
    return cast(dict[str, object], payload)


def _run_mutation(arguments: list[str]) -> None:
    if os.environ.get("GOBBY_TEST_PROTECT") is not None:
        raise UiExposeError("Tailscale mutation blocked by GOBBY_TEST_PROTECT")
    executable = shutil.which("tailscale")
    if executable is None:
        raise UiExposeError("tailscale executable was not found")
    try:
        result = subprocess.run(  # nosec B603 - executable resolved from PATH, fixed arguments
            [executable, *arguments],
            capture_output=True,
            text=True,
            timeout=_MUTATION_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UiExposeError("tailscale command timed out") from exc
    except OSError as exc:
        raise UiExposeError(f"tailscale command failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise UiExposeError(f"tailscale command failed: {detail}")


def _best_effort_remove_root(daemon_port: int) -> None:
    try:
        state = _probe_serve_state()
        if state.root_proxy is None or not _is_gobby_proxy(state.root_proxy, daemon_port):
            return
        _run_mutation(["serve", "--yes", "--https=443", "--set-path=/", "off"])
    except UiExposeError:
        pass


def _best_effort_restore_root(daemon_port: int) -> None:
    try:
        state = _probe_serve_state()
        if state.root_proxy is not None:
            return
        _run_mutation(
            [
                "serve",
                "--yes",
                "--bg",
                "--https=443",
                "--set-path=/",
                _proxy_target(daemon_port),
            ]
        )
    except UiExposeError:
        pass


def _required_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise UiExposeError(f"Tailscale {label} state is malformed")
    return cast(Mapping[str, object], value)


def _optional_mapping(value: object, label: str) -> Mapping[str, object]:
    if value is None:
        return {}
    return _required_mapping(value, label)


def _proxy_target(daemon_port: int) -> str:
    return f"http://localhost:{daemon_port}"


def _is_gobby_proxy(target: str, daemon_port: int) -> bool:
    try:
        parsed = urlsplit(target)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1"}
        and port == daemon_port
        and parsed.path in {"", "/"}
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.username is None
        and parsed.password is None
    )
