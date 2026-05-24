"""
Status message formatting for Gobby daemon.

Provides consistent status display across CLI and MCP server.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Label width for alignment in status sections
_LW = 18
_CODING_CLI_LABELS = (
    ("agy", "AGY CLI"),
    ("claude", "Claude Code"),
    ("codex", "Codex CLI"),
    ("droid", "Droid CLI"),
    ("gemini", "Gemini CLI (deprecated)"),
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
                f"http://localhost:{http_port}/api/admin/status", timeout=timeout
            )
        if response.status_code == 200:
            result: dict[str, Any] = response.json()
            return result
    except (httpx.ConnectError, httpx.TimeoutException):
        pass
    except Exception as e:
        logger.debug(f"Failed to fetch daemon status: {e}")
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
        name for name in ("pg_search", "pgaudit") if name in extensions and not extensions.get(name)
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
    **kwargs: Any,
) -> str:
    """Format the full operational health dashboard for gobby status."""
    lines: list[str] = []
    data = api_data or {}

    lines.append("=" * 70)
    lines.append("GOBBY DAEMON STATUS")
    lines.append("=" * 70)
    lines.append("")

    # ---- Runtime ----
    lines.append("Runtime:")
    if running:
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

    lines.append("")

    # ---- Network ----
    if running and (http_port or websocket_port):
        lines.append("Network:")
        if http_port:
            lines.append(f"  {'HTTP:':<{_LW}}localhost:{http_port}")
        if websocket_port:
            lines.append(f"  {'WebSocket:':<{_LW}}localhost:{websocket_port}")

        # Tailscale
        ts_info = (deps_info or {}).get("dependencies", {}).get("tailscale")
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
                if ui_mode == "dev" and ui_pid:
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
        if gobby.get("gsqz"):
            path_str = f" ({gobby['gsqz_path']})" if gobby.get("gsqz_path") else ""
            lines.append(f"  {'gsqz:':<{_LW}}{gobby['gsqz']}{path_str}")
        elif gobby.get("gsqz") is None:
            lines.append(f"  {'gsqz:':<{_LW}}not installed")
        if gobby.get("ghook"):
            path_str = f" ({gobby['ghook_path']})" if gobby.get("ghook_path") else ""
            lines.append(f"  {'ghook:':<{_LW}}{gobby['ghook']}{path_str}")
        elif gobby.get("ghook") is None:
            lines.append(f"  {'ghook:':<{_LW}}not installed")
        if gobby.get("gloc"):
            path_str = f" ({gobby['gloc_path']})" if gobby.get("gloc_path") else ""
            lines.append(f"  {'gloc:':<{_LW}}{gobby['gloc']}{path_str}")
        elif gobby.get("gloc") is None:
            lines.append(f"  {'gloc:':<{_LW}}not installed")
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
        dep = (deps_info or {}).get("dependencies", {})

        # Docker
        docker_ver = dep.get("docker")
        if docker_ver:
            docker_running = dep.get("docker_running", False)
            status_str = "running" if docker_running else "stopped"
            lines.append(f"  {'Docker:':<{_LW}}{status_str} (v{docker_ver})")

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
        configured_embeddings_provider = dep.get("embeddings_provider")
        ollama = dep.get("ollama")
        lmstudio = dep.get("lmstudio")
        if configured_embeddings_provider == "ollama":
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

        lines.append("")

    # ---- Dependencies ----
    if deps_info and deps_info.get("dependencies"):
        dep = deps_info["dependencies"]
        dep_items: list[tuple[str, str | None]] = [
            ("tmux", dep.get("tmux")),
            ("git", dep.get("git")),
            ("node", dep.get("node")),
        ]
        # Add tailscale version (not the full info dict)
        ts = dep.get("tailscale")
        ts_ver = ts.get("version") if isinstance(ts, dict) else None
        dep_items.append(("tailscale", ts_ver))

        lines.append("Dependencies:")
        for name, version in dep_items:
            if version:
                lines.append(f"  {name + ':':<{_LW}}{version}")
            else:
                lines.append(f"  {name + ':':<{_LW}}not installed")
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
        if health and health not in ("healthy", None):
            health_issues.append(f"MCP: {name} — {health}")
        elif info.get("consecutive_failures", 0) > 0:
            health_issues.append(
                f"MCP: {name} — {info['consecutive_failures']} consecutive failures"
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
