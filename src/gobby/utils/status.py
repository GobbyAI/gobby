"""
Status message formatting for Gobby daemon.

Provides consistent status display across CLI and MCP server.
"""

import logging
from typing import Any

import httpx

from gobby.utils.dependency_requirements import STARTING_GRACE_SECONDS
from gobby.utils.local_token import daemon_auth_headers
from gobby.utils.postgres_extensions import BASELINE_POSTGRES_EXTENSIONS

logger = logging.getLogger(__name__)

# Label width for alignment in status sections
_LW = 18
_CODING_CLI_LABELS = (
    ("agy", "AGY CLI"),
    ("claude", "Claude Code"),
    ("codex", "Codex CLI"),
    ("droid", "Droid CLI"),
    ("grok", "Grok CLI"),
    ("qwen", "Qwen CLI"),
)


async def fetch_rich_status(http_port: int, timeout: float = 3.0) -> dict[str, Any]:
    """Fetch rich status data from the daemon API.

    Returns the raw /api/admin/status response dict, or empty dict on failure.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://localhost:{http_port}/api/admin/status",
                headers=daemon_auth_headers(),
                timeout=timeout,
            )
        if response.status_code == 200:
            result: dict[str, Any] = response.json()
            return result
    except (httpx.ConnectError, httpx.TimeoutException):
        pass
    except Exception as e:
        logger.debug("Failed to fetch daemon status: %s", e)
    return {}


def _format_bytes(n: int) -> str:
    """Format bytes as human-readable size."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


def _safe_status_text(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _format_postgres_host_db(payload: dict[str, Any]) -> str | None:
    host = _safe_status_text(payload.get("dsn_host"))
    db_name = _safe_status_text(payload.get("dsn_db"))
    if host and db_name:
        return f"{host}/{db_name}"
    return host or db_name


def _format_postgres_extensions(payload: dict[str, Any]) -> str | None:
    extensions = payload.get("extensions")
    if not isinstance(extensions, dict) or not extensions:
        return None

    missing = [
        name
        for name in BASELINE_POSTGRES_EXTENSIONS
        if name in extensions and not extensions.get(name)
    ]
    if missing:
        return f"missing {', '.join(missing)}"
    return "extensions ok"


def _format_postgres_service_status(payload: Any) -> str | None:
    """Format a compact PostgreSQL hub service status without exposing DSNs."""
    if not isinstance(payload, dict):
        return None

    mode = _safe_status_text(payload.get("mode")) or "unknown"
    if payload.get("available") is False:
        details = [mode]
        error = _safe_status_text(payload.get("error"))
        if error:
            details.append(error)
        return f"unavailable ({'; '.join(details)})"

    details = [mode]
    for part in (
        _format_postgres_host_db(payload),
        _format_postgres_extensions(payload),
    ):
        if part:
            details.append(part)

    health = "healthy" if payload.get("healthy") else "unhealthy"
    return f"{health} ({'; '.join(details)})"


def _provider_model_count(provider_models: Any, provider: str) -> int | None:
    if not isinstance(provider_models, dict):
        return None
    info = provider_models.get(provider)
    if not isinstance(info, dict):
        return None
    count = info.get("model_count")
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return count
    return None


def _format_coding_cli_details(hooks: dict[str, Any], provider_models: Any, name: str) -> str:
    parts = []
    if hooks.get(name):
        parts.append("hooks installed")
    if name == "agy":
        parts.append("unavailable: no machine transport")

    model_count = _provider_model_count(provider_models, name)
    if model_count is not None:
        plural = "s" if model_count != 1 else ""
        parts.append(f"{model_count} model{plural} available")

    return f" ({', '.join(parts)})" if parts else ""


def _dependency_sections(
    deps_info: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    dependencies = _mapping_section(deps_info, "dependencies")
    return (
        _mapping_section(dependencies, "required"),
        _mapping_section(dependencies, "optional"),
        _mapping_section(deps_info, "runtime"),
    )


def _mapping_section(value: dict[str, Any] | None, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    section = value.get(key)
    return section if isinstance(section, dict) else {}


def _unhealthy_required_dependencies(deps_info: dict[str, Any] | None) -> list[tuple[str, str]]:
    required, _, runtime = _dependency_sections(deps_info)
    unhealthy: list[tuple[str, str]] = []
    for name, record in {**runtime, **required}.items():
        if not isinstance(record, dict) or record.get("state") == "healthy":
            continue
        error = _safe_status_text(record.get("error")) or "dependency is unhealthy"
        unhealthy.append((str(name), error))
    return unhealthy


def _format_dependency_record(record: Any, *, managed: bool = False) -> str:
    if not isinstance(record, dict):
        return "invalid"
    state = _safe_status_text(record.get("state")) or "invalid"
    installed = _safe_status_text(record.get("installed_version"))
    minimum = _safe_status_text(record.get("minimum_version"))
    expected = _safe_status_text(record.get("expected_version"))
    if state == "healthy":
        value = installed or "verified"
        if managed:
            return f"{value} (managed, verified)"
        if minimum:
            return f"{value} (min: {minimum})"
        if expected:
            return f"{value} (expected: {expected})"
        return value
    details: list[str] = []
    if installed:
        details.append(f"detected: {installed}")
    if minimum:
        details.append(f"min: {minimum}")
    if expected:
        details.append(f"expected: {expected}")
    return f"{state} ({'; '.join(details)})" if details else state


def format_status_message(
    *,
    running: bool,
    pid: int | None = None,
    uptime: str | None = None,
    http_port: int | None = None,
    websocket_port: int | None = None,
    service_info: str | None = None,
    # Raw API data (new approach — pass full sections)
    api_data: dict[str, Any] | None = None,
    # UI info
    ui_enabled: bool | None = None,
    ui_mode: str | None = None,
    ui_url: str | None = None,
    ui_pid: int | None = None,
    # Paths
    log_files: str | None = None,
    # Deps info (from collect_all_deps)
    deps_info: dict[str, Any] | None = None,
    # Config mismatches
    config_issues: list[dict[str, str]] | None = None,
    control_plane_error: str | None = None,
    process_uptime_seconds: float | None = None,
    unsupported_platform: bool = False,
    **kwargs: Any,
) -> str:
    """Format the full operational health dashboard for gobby status."""
    lines: list[str] = []
    data = api_data or {}
    unhealthy_dependencies = _unhealthy_required_dependencies(deps_info)
    starting = (
        running
        and control_plane_error is not None
        and not unhealthy_dependencies
        and process_uptime_seconds is not None
        and process_uptime_seconds < STARTING_GRACE_SECONDS
    )

    lines.append("=" * 70)
    lines.append("GOBBY DAEMON STATUS")
    lines.append("=" * 70)
    lines.append("")

    # ---- Runtime ----
    lines.append("Runtime:")
    if unsupported_platform:
        lines.append(f"  {'Status:':<{_LW}}Unsupported (native Windows; use WSL 2)")
    elif running:
        if starting:
            status_str = f"Starting (PID: {pid})" if pid else "Starting"
        elif control_plane_error or unhealthy_dependencies:
            if control_plane_error:
                status_str = f"Degraded (PID: {pid}; HTTP unavailable)" if pid else "Degraded"
            else:
                status_str = f"Degraded (PID: {pid})" if pid else "Degraded"
        else:
            status_str = f"Running (PID: {pid})" if pid else "Running"
        lines.append(f"  {'Status:':<{_LW}}{status_str}")

        if service_info:
            lines.append(f"  {'Install:':<{_LW}}{service_info}")

        if uptime:
            lines.append(f"  {'Uptime:':<{_LW}}{uptime}")

        process = data.get("process")
        if process:
            mem = process.get("memory_rss_mb")
            cpu = process.get("cpu_percent")
            parts = []
            if mem is not None:
                parts.append(f"{mem:.1f} MB")
            if cpu is not None:
                parts.append(f"CPU: {cpu:.1f}%")
            if parts:
                lines.append(f"  {'Memory:':<{_LW}}{' | '.join(parts)}")

        fd = data.get("fd_usage", {})
        if fd.get("current") is not None:
            lines.append(
                f"  {'File descriptors:':<{_LW}}{fd['current']} / {fd.get('soft_limit', '?')}"
            )

        db_size = data.get("db_size_bytes")
        if db_size is not None:
            lines.append(f"  {'Database:':<{_LW}}{_format_bytes(db_size)}")

        last_shutdown = data.get("last_shutdown")
        if last_shutdown:
            lines.append(f"  {'Last shutdown:':<{_LW}}{last_shutdown}")
    else:
        lines.append(f"  {'Status:':<{_LW}}Stopped")

    _, _, runtime_dependencies = _dependency_sections(deps_info)
    python_status = runtime_dependencies.get("python")
    if python_status:
        lines.append(f"  {'Python:':<{_LW}}{_format_dependency_record(python_status)}")

    lines.append("")

    # ---- Network ----
    if running and (http_port or websocket_port):
        lines.append("Network:")
        if http_port:
            lines.append(f"  {'HTTP:':<{_LW}}localhost:{http_port}")
        if websocket_port:
            lines.append(f"  {'WebSocket:':<{_LW}}localhost:{websocket_port}")

        # Tailscale
        ts_info = _mapping_section(deps_info, "integrations").get("tailscale")
        if ts_info and isinstance(ts_info, dict) and ts_info.get("hostname"):
            ts_line = f"https://{ts_info['hostname']}"
            if ts_info.get("serving"):
                ts_line += f" (serving, funnel: {'on' if ts_info.get('funnel') else 'off'})"
            lines.append(f"  {'Tailscale:':<{_LW}}{ts_line}")

        # Web UI
        if ui_enabled and ui_url:
            ui_detail = ui_url
            if ui_mode:
                ui_detail += f" ({ui_mode}"
                if ui_pid:
                    ui_detail += f", PID: {ui_pid}"
                ui_detail += ")"
            lines.append(f"  {'Web UI:':<{_LW}}{ui_detail}")

        lines.append("")

    # ---- Gobby CLIs ----
    if deps_info and deps_info.get("gobby"):
        gobby = deps_info["gobby"]
        lines.append("Gobby:")
        if gobby.get("gobby"):
            lines.append(f"  {'gobby:':<{_LW}}{gobby['gobby']}")
        if gobby.get("gcode"):
            path_str = f" ({gobby['gcode_path']})" if gobby.get("gcode_path") else ""
            lines.append(f"  {'gcode:':<{_LW}}{gobby['gcode']}{path_str}")
        elif gobby.get("gcode") is None:
            lines.append(f"  {'gcode:':<{_LW}}not installed")
        if gobby.get("ghook"):
            path_str = f" ({gobby['ghook_path']})" if gobby.get("ghook_path") else ""
            lines.append(f"  {'ghook:':<{_LW}}{gobby['ghook']}{path_str}")
        elif gobby.get("ghook") is None:
            lines.append(f"  {'ghook:':<{_LW}}not installed")
        if gobby.get("gwiki"):
            path_str = f" ({gobby['gwiki_path']})" if gobby.get("gwiki_path") else ""
            lines.append(f"  {'gwiki:':<{_LW}}{gobby['gwiki']}{path_str}")
        elif gobby.get("gwiki") is None:
            lines.append(f"  {'gwiki:':<{_LW}}not installed")
        lines.append("")

    # ---- Coding CLIs ----
    if deps_info and deps_info.get("coding_clis"):
        clis = deps_info["coding_clis"]
        hooks = clis.get("hooks", {})
        provider_models = data.get("provider_models")
        lines.append("Coding CLIs:")
        for name, label in _CODING_CLI_LABELS:
            version = clis.get(name)
            details = _format_coding_cli_details(hooks, provider_models, name)
            if version:
                lines.append(f"  {label + ':':<{_LW}}{version}{details}")
            else:
                lines.append(f"  {label + ':':<{_LW}}not installed{details}")
        lines.append("")

    # ---- Services ----
    if running:
        lines.append("Services:")
        services = _mapping_section(deps_info, "services")
        integrations = _mapping_section(deps_info, "integrations")

        # Docker Engine and Compose
        docker = services.get("docker") if isinstance(services, dict) else None
        if docker:
            docker_detail = _format_dependency_record(docker)
            if isinstance(docker, dict) and docker.get("state") == "healthy":
                docker_state = "running" if services.get("docker_running") else "stopped"
                docker_detail = f"{docker_detail} ({docker_state})"
            lines.append(f"  {'Docker Engine:':<{_LW}}{docker_detail}")
        compose = services.get("docker_compose") if isinstance(services, dict) else None
        if compose:
            lines.append(f"  {'Docker Compose:':<{_LW}}{_format_dependency_record(compose)}")

        # PostgreSQL hub
        postgres_status = _format_postgres_service_status(data.get("postgres"))
        if postgres_status:
            lines.append(f"  {'PostgreSQL:':<{_LW}}{postgres_status}")

        # Qdrant
        memory = data.get("memory", {})
        qdrant = memory.get("qdrant", {})
        if qdrant.get("configured"):
            status_str = "healthy" if qdrant.get("healthy") else "unhealthy"
            lines.append(f"  {'Qdrant:':<{_LW}}{status_str}")

        # FalkorDB
        falkordb = memory.get("falkordb", {})
        if falkordb.get("configured") or falkordb.get("installed"):
            url_str = f" ({falkordb['url']})" if falkordb.get("url") else ""
            if falkordb.get("healthy"):
                lines.append(f"  {'FalkorDB:':<{_LW}}healthy{url_str}")
            elif falkordb.get("configured") and falkordb.get("installed"):
                lines.append(f"  {'FalkorDB:':<{_LW}}not responding{url_str}")
            elif falkordb.get("installed"):
                lines.append(f"  {'FalkorDB:':<{_LW}}installed, not configured{url_str}")
            else:
                lines.append(f"  {'FalkorDB:':<{_LW}}not installed")

        # Embeddings
        configured_embeddings_provider = integrations.get("embeddings_provider")
        ollama = integrations.get("ollama")
        lmstudio = integrations.get("lmstudio")
        if (
            isinstance(configured_embeddings_provider, dict)
            and configured_embeddings_provider.get("status") == "degraded"
        ):
            error = _safe_status_text(configured_embeddings_provider.get("error"))
            detail = f" ({error})" if error else ""
            lines.append(f"  {'Embeddings:':<{_LW}}degraded{detail}")
        elif configured_embeddings_provider == "ollama":
            if isinstance(ollama, dict) and ollama.get("running"):
                ver_str = f" (v{ollama['version']})" if ollama.get("version") else ""
                lines.append(f"  {'Embeddings:':<{_LW}}Ollama{ver_str}")
            elif isinstance(ollama, dict):
                lines.append(f"  {'Embeddings:':<{_LW}}Ollama (stopped)")
            else:
                lines.append(f"  {'Embeddings:':<{_LW}}Ollama")
        elif configured_embeddings_provider == "lmstudio":
            if isinstance(lmstudio, dict) and lmstudio.get("running"):
                lines.append(f"  {'Embeddings:':<{_LW}}LM Studio (running)")
            elif isinstance(lmstudio, dict):
                lines.append(f"  {'Embeddings:':<{_LW}}LM Studio (stopped)")
            else:
                lines.append(f"  {'Embeddings:':<{_LW}}LM Studio")
        elif configured_embeddings_provider == "openai":
            lines.append(f"  {'Embeddings:':<{_LW}}OpenAI")
        elif configured_embeddings_provider == "vllm":
            lines.append(f"  {'Embeddings:':<{_LW}}vLLM")
        elif configured_embeddings_provider == "openai-compatible":
            lines.append(f"  {'Embeddings:':<{_LW}}OpenAI-compatible endpoint")
        elif configured_embeddings_provider == "none":
            lines.append(f"  {'Embeddings:':<{_LW}}disabled")
        elif isinstance(ollama, dict) and ollama.get("running"):
            ver_str = f" (v{ollama['version']})" if ollama.get("version") else ""
            lines.append(f"  {'Embeddings:':<{_LW}}Ollama{ver_str}")
        elif isinstance(lmstudio, dict) and lmstudio.get("running"):
            lines.append(f"  {'Embeddings:':<{_LW}}LM Studio (running)")
        elif isinstance(ollama, dict):
            lines.append(f"  {'Embeddings:':<{_LW}}Ollama (stopped)")
        elif isinstance(lmstudio, dict):
            lines.append(f"  {'Embeddings:':<{_LW}}LM Studio (stopped)")

        generation_endpoints = data.get("generation_endpoints")
        if isinstance(generation_endpoints, list):
            printed = 0
            for entry in generation_endpoints:
                if not isinstance(entry, dict):
                    continue
                name = _safe_status_text(entry.get("name")) or "endpoint"
                provider_label = _safe_status_text(entry.get("provider_label"))
                api_base = _safe_status_text(entry.get("api_base")) or ""
                origin = api_base.rstrip("/")
                if origin.endswith("/v1"):
                    origin = origin[: -len("/v1")]
                if entry.get("healthy"):
                    served = (
                        _safe_status_text(entry.get("served_model"))
                        or _safe_status_text(entry.get("model"))
                        or "unknown model"
                    )
                    state = f"healthy, {served}"
                else:
                    error = _safe_status_text(entry.get("error")) or "unknown error"
                    if len(error) > 80:
                        error = f"{error[:77]}..."
                    state = f"unreachable ({error})"
                identity = f"{name} ({provider_label})" if provider_label else name
                detail = f"{identity} {origin} — {state}" if origin else f"{identity} — {state}"
                row_label = "Generation:" if printed == 0 else ""
                lines.append(f"  {row_label:<{_LW}}{detail}")
                printed += 1

        automation_loop = (
            data.get("system_services", {}).get("automation_loop")
            if isinstance(data.get("system_services"), dict)
            else None
        )
        if isinstance(automation_loop, dict):
            enabled = automation_loop.get("enabled")
            running_loop = automation_loop.get("running")
            interval = automation_loop.get("interval_seconds")
            status_str = "running" if running_loop else "stopped"
            if enabled is False:
                status_str = "disabled"
            detail = f"{status_str}"
            if interval is not None:
                detail += f" (every {interval}s)"
            lines.append(f"  {'Automation:':<{_LW}}{detail}")

        lines.append("")

    # ---- Dependencies ----
    required_dependencies, optional_dependencies, _ = _dependency_sections(deps_info)
    labels = {
        "tmux": "tmux",
        "git": "git",
        "node": "node",
        "srt": "SRT",
        "impeccable": "Impeccable",
    }
    managed_dependencies = {"srt", "impeccable"}
    if required_dependencies:
        lines.append("Required Dependencies:")
        for name, record in required_dependencies.items():
            if name == "docker_compose":
                continue
            label = labels.get(name, name)
            lines.append(
                f"  {label + ':':<{_LW}}"
                f"{_format_dependency_record(record, managed=name in managed_dependencies)}"
            )
        lines.append("")

    if optional_dependencies:
        lines.append("Optional Dependencies:")
        for name, record in optional_dependencies.items():
            label = labels.get(name, name)
            lines.append(
                f"  {label + ':':<{_LW}}"
                f"{_format_dependency_record(record, managed=name in managed_dependencies)}"
            )
        lines.append("")

    # ---- Active Work (only if non-zero) ----
    if running:
        sessions = data.get("sessions", {})
        agents = data.get("agents", {})
        pipelines = data.get("pipelines", {})

        active_parts: list[tuple[str, str]] = []
        s_active = sessions.get("active", 0)
        s_paused = sessions.get("paused", 0)
        if s_active or s_paused:
            parts = []
            if s_active:
                parts.append(f"{s_active} active")
            if s_paused:
                parts.append(f"{s_paused} paused")
            active_parts.append(("Sessions", ", ".join(parts)))

        a_running = agents.get("running", 0)
        if a_running:
            active_parts.append(("Agents", f"{a_running} running"))

        p_running = pipelines.get("running", 0)
        p_waiting = pipelines.get("waiting_approval", 0)
        if p_running or p_waiting:
            parts = []
            if p_running:
                parts.append(f"{p_running} running")
            if p_waiting:
                parts.append(f"{p_waiting} waiting approval")
            active_parts.append(("Pipelines", ", ".join(parts)))

        if active_parts:
            lines.append("Active Work:")
            for label, detail in active_parts:
                lines.append(f"  {label + ':':<{_LW}}{detail}")
            lines.append("")

    # ---- Health Issues (only if problems exist) ----
    health_issues: list[str] = []

    if control_plane_error and not starting:
        health_issues.append(f"Daemon control plane: {control_plane_error}")

    for name, error in unhealthy_dependencies:
        health_issues.append(f"Required dependency {name}: {error}")

    degraded_services = data.get("degraded_services")
    if isinstance(degraded_services, list):
        for service_name in degraded_services:
            service = _safe_status_text(service_name)
            if service:
                health_issues.append(f"Degraded service: {service}")

    hook_runtime = data.get("hook_runtime")
    if isinstance(hook_runtime, dict):
        runtime_state = _safe_status_text(hook_runtime.get("state"))
        if runtime_state not in {None, "absent", "compatible"}:
            runtime_detail = _safe_status_text(hook_runtime.get("detail"))
            hook_issue = f"Hook runtime: {runtime_state}"
            if runtime_detail:
                hook_issue += f" — {runtime_detail}"
            health_issues.append(hook_issue)

    # Config mismatches
    if config_issues:
        for issue in config_issues:
            health_issues.append(f"{issue['subsystem']}: {issue['error']}")

    # MCP unhealthy
    mcp_servers = data.get("mcp_servers", {})
    for name, info in mcp_servers.items():
        if info.get("internal"):
            continue
        health = info.get("health")
        last_error = _safe_status_text(info.get("last_error"))
        error_suffix = f": {last_error}" if last_error else ""
        if health and health not in ("healthy", None):
            health_issues.append(f"MCP: {name} — {health}{error_suffix}")
        elif info.get("consecutive_failures", 0) > 0:
            health_issues.append(
                f"MCP: {name} — {info['consecutive_failures']} consecutive failures{error_suffix}"
            )

    provider_models = data.get("provider_models", {})
    for name, info in provider_models.items():
        source = info.get("source")
        error = info.get("error")
        if source == "cache":
            health_issues.append(
                f"Provider models: {name} — using cache ({error or 'probe failed'})"
            )
        elif source == "failed":
            health_issues.append(f"Provider models: {name} — {error or 'discovery failed'}")

    postgres = data.get("postgres")
    if isinstance(postgres, dict):
        if postgres.get("available") is False:
            error = _safe_status_text(postgres.get("error")) or "status unavailable"
            health_issues.append(f"PostgreSQL: {error}")
        elif postgres.get("healthy") is False:
            health_issues.append("PostgreSQL: unhealthy")

    if health_issues:
        lines.append("Health Issues:")
        for issue_msg in health_issues:
            lines.append(f"  ! {issue_msg}")
        lines.append("")

    # ---- Footer ----
    lines.append("=" * 70)

    return "\n".join(lines)


def format_startup_summary(
    *,
    pid: int,
    http_port: int,
    websocket_port: int,
    ui_url: str | None = None,
    ui_mode: str | None = None,
    log_files: str | None = None,
) -> str:
    """Compact summary shown after daemon startup."""
    lines = [f"Gobby daemon ready (PID: {pid})"]
    lines.append(f"  HTTP:      localhost:{http_port}")
    lines.append(f"  WebSocket: localhost:{websocket_port}")
    if ui_url:
        mode_str = f" ({ui_mode})" if ui_mode else ""
        lines.append(f"  Web UI:    {ui_url}{mode_str}")
    if log_files:
        lines.append(f"  Logs:      {log_files}")
    return "\n".join(lines)
