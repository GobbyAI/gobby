"""Tailscale Serve and Funnel exposure of the Web UI and the /mcp route.

The operator's choices live in hub-backed config (`ui_exposure`, `mcp_exposure`).
This module applies them to the node's Tailscale state at daemon start and when the
operator changes them. It adopts equivalent manual handlers, refuses handlers it does
not own, and never touches a serve handler outside the ports and paths it manages.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 # fixed tailscale CLI commands
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import urlsplit

from gobby.config.app import McpExposureConfig, UiExposureConfig
from gobby.utils.env import is_test_protect_enabled

_READ_TIMEOUT_SECONDS = 5.0
_MUTATION_TIMEOUT_SECONDS = 15.0
_UI_PATH = "/"
_MCP_PATH = "/mcp"

PersistIntent = Callable[[], None]


class UiExposeError(RuntimeError):
    """Raised when exposure cannot be safely inspected or changed."""


@dataclass(frozen=True)
class UiExposeResult:
    """Current or resulting exposure state for the Web UI or the /mcp route."""

    enabled: bool
    url: str | None = None
    changed: bool = False
    degraded_reason: str | None = None
    funnel: bool = False


@dataclass(frozen=True)
class _Route:
    """One Gobby-managed Serve handler: a path on one HTTPS port of this node."""

    port: int
    path: str
    funnel: bool

    def target(self, daemon_port: int) -> str:
        return f"http://localhost:{daemon_port}{self.path if self.path == _MCP_PATH else ''}"


@dataclass(frozen=True)
class _NodeInfo:
    dns_name: str

    def url(self, port: int, path: str) -> str:
        authority = self.dns_name if port == 443 else f"{self.dns_name}:{port}"
        return f"https://{authority}{path}"


@dataclass(frozen=True)
class _ServeState:
    node: _NodeInfo
    tcp: Mapping[int, object]
    handlers: Mapping[int, Mapping[str, str]]
    funnel_ports: frozenset[int]


def _ui_route(port: int) -> _Route:
    return _Route(port=port, path=_UI_PATH, funnel=False)


def _mcp_route(port: int, *, mode: Literal["tailnet", "funnel"]) -> _Route:
    return _Route(port=port, path=_MCP_PATH, funnel=mode == "funnel")


def enable_tailscale_ui(
    daemon_port: int,
    *,
    port: int = 443,
    persist: PersistIntent | None = None,
) -> UiExposeResult:
    """Serve the Web UI root on a tailnet-only HTTPS port and record it with `persist`."""
    return _apply_route(daemon_port, _ui_route(port), persist)


def disable_tailscale_ui(
    daemon_port: int,
    *,
    port: int = 443,
    forget: bool = False,
    persist: PersistIntent | None = None,
) -> UiExposeResult:
    """Remove the managed Web UI handler, or only forget the recorded intent."""
    return _remove_route(daemon_port, _ui_route(port), persist, forget=forget)


def enable_mcp_exposure(
    daemon_port: int,
    *,
    mode: Literal["tailnet", "funnel"],
    port: int,
    persist: PersistIntent | None = None,
) -> UiExposeResult:
    """Serve only the /mcp path on `port`, optionally published with Funnel."""
    return _apply_route(daemon_port, _mcp_route(port, mode=mode), persist)


def disable_mcp_exposure(
    daemon_port: int,
    *,
    mode: Literal["tailnet", "funnel"],
    port: int,
    forget: bool = False,
    persist: PersistIntent | None = None,
) -> UiExposeResult:
    """Remove the managed /mcp handler, or only forget the recorded intent."""
    return _remove_route(daemon_port, _mcp_route(port, mode=mode), persist, forget=forget)


def reconcile_exposures(
    daemon_port: int,
    *,
    ui: UiExposureConfig,
    mcp: McpExposureConfig,
) -> list[UiExposeResult]:
    """Re-derive every recorded handler on this node at daemon start.

    Each failure becomes a degraded result instead of an exception, so one broken
    exposure cannot block daemon start or the other exposure.
    """
    results: list[UiExposeResult] = []
    if ui.mode == "tailnet":
        results.append(
            _degraded(
                funnel=False,
                action=lambda: enable_tailscale_ui(daemon_port, port=ui.port),
            )
        )
    if mcp.mode != "off":
        mcp_mode = mcp.mode
        results.append(
            _degraded(
                funnel=mcp_mode == "funnel",
                action=lambda: enable_mcp_exposure(daemon_port, mode=mcp_mode, port=mcp.port),
            )
        )
    return results


def get_exposure_status(
    daemon_port: int,
    *,
    ui: UiExposureConfig,
    mcp: McpExposureConfig,
) -> tuple[UiExposeResult, UiExposeResult]:
    """Return the recorded UI and /mcp exposure state, reading Tailscale without changes."""
    ui_status = (
        _status(daemon_port, _ui_route(ui.port))
        if ui.mode == "tailnet"
        else UiExposeResult(enabled=False)
    )
    mcp_status = (
        _status(daemon_port, _mcp_route(mcp.port, mode=mcp.mode))
        if mcp.mode != "off"
        else UiExposeResult(enabled=False)
    )
    return ui_status, mcp_status


def node_dns_name() -> str:
    """Return this node's MagicDNS name from `tailscale status`."""
    return _read_node_info().dns_name


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
    port: int = 443,
    persist: PersistIntent | None = None,
) -> UiExposeResult | None:
    """Apply an affirmative installer exposure choice."""
    if not expose:
        return None
    return enable_tailscale_ui(daemon_port, port=port, persist=persist)


def _apply_route(
    daemon_port: int,
    route: _Route,
    persist: PersistIntent | None,
) -> UiExposeResult:
    state = _probe_serve_state()
    _assert_port_usable(state, route)
    current = state.handlers.get(route.port, {}).get(route.path)
    if current is not None:
        if not _is_gobby_target(current, route, daemon_port):
            raise UiExposeError(
                f"the Tailscale HTTPS {route.path} handler on port {route.port} "
                "is owned by another proxy"
            )
        if not route.funnel or route.port in state.funnel_ports:
            _run_persist(persist)
            return _enabled_result(state.node, route, changed=False)

    _run_mutation(_mutation_args(route, daemon_port, off=False))
    try:
        verified = _probe_serve_state()
        if not _route_applied(verified, route, daemon_port):
            raise UiExposeError(
                f"Tailscale did not install the expected HTTPS {route.path} handler "
                f"on port {route.port}"
            )
        _run_persist(persist)
    except Exception:
        _best_effort_remove(daemon_port, route)
        raise
    return _enabled_result(verified.node, route, changed=True)


def _remove_route(
    daemon_port: int,
    route: _Route,
    persist: PersistIntent | None,
    *,
    forget: bool,
) -> UiExposeResult:
    if forget:
        _run_persist(persist)
        return UiExposeResult(enabled=False)

    state = _probe_serve_state()
    current = state.handlers.get(route.port, {}).get(route.path)
    if current is None:
        _run_persist(persist)
        return UiExposeResult(enabled=False)
    if not _is_gobby_target(current, route, daemon_port):
        raise UiExposeError(
            f"the Tailscale HTTPS {route.path} handler on port {route.port} "
            "is owned by another proxy"
        )

    _run_mutation(_mutation_args(route, daemon_port, off=True))
    try:
        verified = _probe_serve_state()
        if verified.handlers.get(route.port, {}).get(route.path) is not None:
            raise UiExposeError(f"Tailscale did not remove the HTTPS {route.path} handler")
        if route.funnel and route.port in verified.funnel_ports:
            if not verified.handlers.get(route.port):
                raise UiExposeError(f"Tailscale Funnel is still active on port {route.port}")
        _run_persist(persist)
    except Exception:
        _best_effort_restore(daemon_port, route)
        raise
    return UiExposeResult(enabled=False, changed=True)


def _status(daemon_port: int, route: _Route) -> UiExposeResult:
    try:
        state = _probe_serve_state()
    except UiExposeError as exc:
        return UiExposeResult(enabled=True, degraded_reason=str(exc), funnel=route.funnel)
    current = state.handlers.get(route.port, {}).get(route.path)
    if current is None:
        reason = f"HTTPS {route.path} handler is missing on port {route.port}"
    elif not _is_gobby_target(current, route, daemon_port):
        reason = f"HTTPS {route.path} handler on port {route.port} points to another proxy"
    elif route.funnel and route.port not in state.funnel_ports:
        reason = f"Tailscale Funnel is not active on port {route.port}"
    else:
        return _enabled_result(state.node, route, changed=False)
    return UiExposeResult(enabled=True, degraded_reason=reason, funnel=route.funnel)


def _degraded(*, funnel: bool, action: Callable[[], UiExposeResult]) -> UiExposeResult:
    try:
        return action()
    except UiExposeError as exc:
        return UiExposeResult(enabled=True, degraded_reason=str(exc), funnel=funnel)


def _enabled_result(node: _NodeInfo, route: _Route, *, changed: bool) -> UiExposeResult:
    return UiExposeResult(
        enabled=True,
        url=node.url(route.port, route.path),
        changed=changed,
        funnel=route.funnel,
    )


def _route_applied(state: _ServeState, route: _Route, daemon_port: int) -> bool:
    current = state.handlers.get(route.port, {}).get(route.path)
    if current is None or not _is_gobby_target(current, route, daemon_port):
        return False
    return not route.funnel or route.port in state.funnel_ports


def _assert_port_usable(state: _ServeState, route: _Route) -> None:
    tcp_value = state.tcp.get(route.port)
    existing = state.handlers.get(route.port, {})
    if tcp_value is not None:
        mapping = _required_mapping(tcp_value, f'TCP["{route.port}"]')
        if mapping.get("HTTPS") is not True or set(mapping) != {"HTTPS"}:
            raise UiExposeError(f"Tailscale port {route.port} has conflicting protocol state")
    elif existing:
        raise UiExposeError(f"Tailscale handlers exist on port {route.port} without HTTPS state")
    if route.funnel:
        foreign = sorted(path for path in existing if path != _MCP_PATH)
        if foreign:
            raise UiExposeError(
                f"Funnel port {route.port} would carry the non-/mcp route {foreign[0]}; refusing"
            )
    elif route.port in state.funnel_ports:
        raise UiExposeError(
            f"Tailscale Funnel is active for this node on port {route.port}; "
            f"refusing to serve {route.path} there"
        )


def _is_gobby_target(target: str, route: _Route, daemon_port: int) -> bool:
    try:
        parsed = urlsplit(target)
        port = parsed.port
    except ValueError:
        return False
    if not (
        parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1"}
        and port == daemon_port
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.username is None
        and parsed.password is None
    ):
        return False
    if route.path == _MCP_PATH:
        return parsed.path == _MCP_PATH
    return parsed.path in {"", "/"}


def _mutation_args(route: _Route, daemon_port: int, *, off: bool) -> list[str]:
    https = f"--https={route.port}"
    set_path = f"--set-path={route.path}"
    if route.funnel:
        if off:
            return ["funnel", https, set_path, "off"]
        return ["funnel", "--bg", https, set_path, route.target(daemon_port)]
    if off:
        return ["serve", "--yes", https, set_path, "off"]
    return ["serve", "--yes", "--bg", https, set_path, route.target(daemon_port)]


def _run_persist(persist: PersistIntent | None) -> None:
    if persist is None:
        return
    try:
        persist()
    except Exception as exc:
        raise UiExposeError(f"failed to persist exposure intent: {exc}") from exc


def _best_effort_remove(daemon_port: int, route: _Route) -> None:
    try:
        state = _probe_serve_state()
        current = state.handlers.get(route.port, {}).get(route.path)
        if current is None or not _is_gobby_target(current, route, daemon_port):
            return
        _run_mutation(_mutation_args(route, daemon_port, off=True))
    except UiExposeError:
        pass


def _best_effort_restore(daemon_port: int, route: _Route) -> None:
    try:
        state = _probe_serve_state()
        if state.handlers.get(route.port, {}).get(route.path) is not None:
            return
        _run_mutation(_mutation_args(route, daemon_port, off=False))
    except UiExposeError:
        pass


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


def _probe_serve_state() -> _ServeState:
    node = _read_node_info()
    config = _run_json(["serve", "status", "--json"])
    tcp = _optional_mapping(config.get("TCP"), "TCP")
    web = _optional_mapping(config.get("Web"), "Web")
    allow_funnel = _optional_mapping(config.get("AllowFunnel"), "AllowFunnel")
    prefix = f"{node.dns_name}:"

    handlers: dict[int, dict[str, str]] = {}
    for host_port, host_config in web.items():
        if not host_port.startswith(prefix):
            continue
        host_mapping = _required_mapping(host_config, f'Web["{host_port}"]')
        port_handlers: dict[str, str] = {}
        for path, handler in _optional_mapping(host_mapping.get("Handlers"), "Handlers").items():
            mapping = _required_mapping(handler, f'Handlers["{path}"]')
            if set(mapping) != {"Proxy"} or not isinstance(mapping.get("Proxy"), str):
                raise UiExposeError(f"the Tailscale handler for {path} is not a proxy")
            port_handlers[path] = cast(str, mapping["Proxy"])
        handlers[_port_of(host_port)] = port_handlers

    funnel_ports: set[int] = set()
    for host_port, value in allow_funnel.items():
        if not host_port.startswith(prefix):
            continue
        if not isinstance(value, bool):
            raise UiExposeError("Tailscale AllowFunnel state is malformed")
        if value:
            funnel_ports.add(_port_of(host_port))

    return _ServeState(
        node=node,
        tcp={_port_of(key): value for key, value in tcp.items()},
        handlers=handlers,
        funnel_ports=frozenset(funnel_ports),
    )


def _port_of(key: str) -> int:
    """Read the port from a `HOST:PORT` Web key or a bare TCP port key."""
    try:
        return int(key.rsplit(":", 1)[-1])
    except ValueError as exc:
        raise UiExposeError(f"Tailscale returned a malformed port key {key!r}") from exc


def _run_json(arguments: list[str]) -> dict[str, object]:
    executable = shutil.which("tailscale")
    if executable is None:
        raise UiExposeError("tailscale executable was not found")
    try:
        result = subprocess.run(  # nosec B603 # executable resolved from PATH, fixed arguments
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
    if is_test_protect_enabled():
        raise UiExposeError("Tailscale mutation blocked by GOBBY_TEST_PROTECT")
    executable = shutil.which("tailscale")
    if executable is None:
        raise UiExposeError("tailscale executable was not found")
    try:
        result = subprocess.run(  # nosec B603 # executable resolved from PATH, fixed arguments
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


def _required_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise UiExposeError(f"Tailscale {label} state is malformed")
    return cast(Mapping[str, object], value)


def _optional_mapping(value: object, label: str) -> Mapping[str, object]:
    if value is None:
        return {}
    return _required_mapping(value, label)
