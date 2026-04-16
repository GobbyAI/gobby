#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx",
#     "pyyaml",
#     "aiofiles",
# ]
# ///
"""Unified Hook Dispatcher - Routes CLI hooks to Gobby daemon.

Supports Claude Code, Gemini CLI, Qwen CLI, and Codex.
CLI is identified via --cli flag (primary) or path-based detection (fallback).

Usage:
    hook_dispatcher.py --cli=claude --type=session-start < input.json
    hook_dispatcher.py --cli=gemini --type=SessionStart --debug < input.json

Exit Codes:
    0 - Success / Allow
    1 - General error (logged, continues)
    2 - Block / Deny operation
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiofiles


# NOTE: This dispatcher runs in an isolated PEP-723 `uv run` environment
# (see the script header above — only httpx, pyyaml, aiofiles are declared).
# The gobby package is NOT importable here. Keep this file self-contained:
# do not add `from gobby.<anything>` imports. Inline helpers instead.
def parse_tmux_socket_path(tmux_env: str | None) -> str | None:
    """Extract the tmux socket path from the ``TMUX`` env var.

    Mirrors ``gobby.sessions.tmux_context.parse_tmux_socket_path`` — must
    stay in sync. Inlined here because the dispatcher cannot import from
    the gobby package at runtime.
    """
    if not isinstance(tmux_env, str):
        return None
    socket_path = tmux_env.split(",", 1)[0].strip()
    return socket_path or None


# Default daemon configuration
DEFAULT_DAEMON_PORT = 60887
DEFAULT_BIND_HOST = "localhost"
DEFAULT_BOOTSTRAP_PATH = "~/.gobby/bootstrap.yaml"

# Wildcard listen-addresses that can't be dialed — normalize to loopback.
_UNROUTABLE_BIND_HOSTS: frozenset[str] = frozenset({"0.0.0.0", "::", "::0"})

_cached_daemon_url: str | None = None


def _resolve_daemon_host(bind_host: str) -> str:
    """Normalize a daemon bind_host for client dialing.

    Wildcard listen-addresses (``0.0.0.0``, ``::``, ``::0``) aren't dialable —
    map to ``127.0.0.1``. IPv6 literals get bracketed for URL syntax.
    Everything else (``localhost``, hostnames, IPv4 literals) passes through.
    """
    if bind_host in _UNROUTABLE_BIND_HOSTS:
        return "127.0.0.1"
    if ":" in bind_host and not bind_host.startswith("["):
        return f"[{bind_host}]"
    return bind_host


# ── CLI Configuration Registry ──────────────────────────────────────────


@dataclass(frozen=True)
class CLIConfig:
    """Per-CLI hook dispatcher configuration.

    Captures the behavioral differences between CLIs while keeping
    all shared logic in common functions.
    """

    source: str  # Source identifier sent to daemon
    critical_hooks: frozenset[str]  # Hooks requiring daemon to be running
    session_start_hooks: frozenset[str]  # Hooks that get terminal context injected
    terminal_context_hooks: frozenset[str]  # Hooks that should carry terminal/process metadata
    json_error_exit_code: int  # Exit code for JSON parse errors (1 or 2)
    logger_name: str  # Logger name for this CLI's dispatcher
    suppress_logs: bool  # Whether to suppress logs in non-debug mode
    has_source_detection: bool  # Whether to detect source from env vars (Claude only)


# Fire-and-forget hooks: spawn a detached curl process and return immediately.
# This prevents Claude Code from cancelling the hook during /exit — the curl
# child survives parent death and delivers the payload to the daemon.
_FIRE_AND_FORGET_HOOKS: frozenset[str] = frozenset({"session-end", "SessionEnd", "sessionEnd"})

CLI_CONFIGS: dict[str, CLIConfig] = {
    "claude": CLIConfig(
        source="claude",
        critical_hooks=frozenset({"session-start", "session-end", "pre-compact"}),
        session_start_hooks=frozenset({"session-start"}),
        terminal_context_hooks=frozenset({"session-start"}),
        json_error_exit_code=2,
        logger_name="gobby.hooks.dispatcher",
        suppress_logs=True,
        has_source_detection=True,
    ),
    "gemini": CLIConfig(
        source="gemini",
        critical_hooks=frozenset({"SessionStart"}),
        session_start_hooks=frozenset({"SessionStart"}),
        terminal_context_hooks=frozenset({"SessionStart"}),
        json_error_exit_code=1,
        logger_name="gobby.hooks.gemini.dispatcher",
        suppress_logs=False,
        has_source_detection=False,
    ),
    "qwen": CLIConfig(
        source="qwen",
        critical_hooks=frozenset({"SessionStart"}),
        session_start_hooks=frozenset({"SessionStart"}),
        terminal_context_hooks=frozenset({"SessionStart"}),
        json_error_exit_code=1,
        logger_name="gobby.hooks.qwen.dispatcher",
        suppress_logs=False,
        has_source_detection=False,
    ),
    "codex": CLIConfig(
        source="codex",
        critical_hooks=frozenset({"SessionStart", "Stop"}),
        session_start_hooks=frozenset({"SessionStart"}),
        terminal_context_hooks=frozenset(
            {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}
        ),
        json_error_exit_code=2,
        logger_name="gobby.hooks.dispatcher.codex",
        suppress_logs=True,
        has_source_detection=False,
    ),
}


def detect_cli(args: argparse.Namespace) -> CLIConfig:
    """Detect CLI from --cli flag or script path.

    Primary: --cli argument (added to hook template commands).
    Fallback: path-based detection from sys.argv[0] for old installations.
    Default: claude (most conservative critical_hooks set).
    """
    # Primary: --cli argument
    if args.cli:
        cli_name = args.cli.lower()
        if cli_name in CLI_CONFIGS:
            return CLI_CONFIGS[cli_name]

    # Fallback: path-based detection from argv[0]
    script_path = sys.argv[0]
    for cli_name in CLI_CONFIGS:
        if f".{cli_name}/" in script_path or f"/{cli_name}/" in script_path:
            return CLI_CONFIGS[cli_name]

    # Default to claude (most conservative)
    return CLI_CONFIGS["claude"]


# ── Daemon URL Resolution ───────────────────────────────────────────────


async def get_daemon_url() -> str:
    """Get the daemon HTTP URL from bootstrap config.

    Reads ``daemon_port`` and ``bind_host`` from ``~/.gobby/bootstrap.yaml``
    when present; otherwise falls back to ``localhost:60887``. Wildcard
    listen-addresses in ``bind_host`` are normalized to ``127.0.0.1`` for
    dialing. Result is cached for the lifetime of the process.

    Returns:
        Full daemon URL like ``http://localhost:60887`` or
        ``http://127.0.0.1:60887`` (when bind_host is a wildcard).
    """
    global _cached_daemon_url
    if _cached_daemon_url is not None:
        return _cached_daemon_url

    bootstrap_path = Path(DEFAULT_BOOTSTRAP_PATH).expanduser()

    port: int = DEFAULT_DAEMON_PORT
    bind_host: str = DEFAULT_BIND_HOST

    if bootstrap_path.exists():
        try:
            import yaml

            async with aiofiles.open(bootstrap_path, encoding="utf-8") as f:
                content = await f.read()
            config = yaml.safe_load(content) or {}
            port = config.get("daemon_port", DEFAULT_DAEMON_PORT)
            bind_host = config.get("bind_host", DEFAULT_BIND_HOST)
        except Exception:
            pass  # Fall back to defaults on any read/parse error.

    host = _resolve_daemon_host(bind_host)
    _cached_daemon_url = f"http://{host}:{port}"
    return _cached_daemon_url


# ── Terminal Context ────────────────────────────────────────────────────


def get_terminal_context() -> dict[str, str | int | bool | None]:
    """Capture terminal/process context for session correlation.

    Returns the superset of all CLI-specific context fields.
    """
    context: dict[str, str | int | bool | None] = {}

    # Parent process ID (shell or CLI process)
    try:
        context["parent_pid"] = os.getppid()
    except Exception:
        context["parent_pid"] = None

    # TTY device name
    try:
        context["tty"] = os.ttyname(0)
    except Exception:
        context["tty"] = None

    # Tmux pane (only if actually running INSIDE a tmux session)
    # IMPORTANT: Only report TMUX_PANE if TMUX env var is also set.
    # The TMUX_PANE env var can be inherited by child processes that are
    # spawned into different terminals (e.g., Ghostty), which would cause
    # kill_agent to kill the parent's tmux pane instead of the child's terminal.
    if os.environ.get("TMUX"):
        context["tmux_pane"] = os.environ.get("TMUX_PANE")
        context["tmux_socket_path"] = parse_tmux_socket_path(os.environ.get("TMUX"))
    else:
        context["tmux_pane"] = None
        context["tmux_socket_path"] = None

    # Generic terminal program identifier (set by many terminals)
    term_program = os.environ.get("TERM_PROGRAM")
    context["term_program"] = term_program

    # Gobby agent context (set by spawn_executor for terminal-mode agents)
    context["gobby_session_id"] = os.environ.get("GOBBY_SESSION_ID")
    context["gobby_parent_session_id"] = os.environ.get("GOBBY_PARENT_SESSION_ID")
    context["gobby_agent_run_id"] = os.environ.get("GOBBY_AGENT_RUN_ID")
    context["gobby_project_id"] = os.environ.get("GOBBY_PROJECT_ID")
    context["gobby_workflow_name"] = os.environ.get("GOBBY_WORKFLOW_NAME")

    return context


# ── Source Detection ────────────────────────────────────────────────────


def _detect_source(config: CLIConfig) -> str:
    """Detect the session source.

    Claude checks GOBBY_SOURCE and CLAUDE_CODE_ENTRYPOINT env vars to
    distinguish SDK vs direct CLI usage. Other CLIs return config.source.
    """
    if not config.has_source_detection:
        return config.source

    # Claude-specific: check GOBBY_SOURCE first (set explicitly by Gobby),
    # then CLAUDE_CODE_ENTRYPOINT (set by Agents SDK)
    gobby_source = os.environ.get("GOBBY_SOURCE")
    if gobby_source:
        return gobby_source
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "sdk-py":
        return "claude"
    return config.source


# ── Block Detection (unified across all CLIs) ──────────────────────────


def is_blocked(result: dict[str, Any]) -> bool:
    """Check if daemon response indicates a block/deny.

    Checks all block patterns used across CLIs:
    - continue=False (Claude, Gemini)
    - decision in ("deny", "block") (Claude, Gemini)
    - hookSpecificOutput.permissionDecision="deny" (Codex PreToolUse)
    """
    if result.get("continue") is False:
        return True
    decision = result.get("decision", "")
    if decision in ("deny", "block"):
        return True
    if result.get("permissionDecision") == "deny":
        return True
    hook_specific = result.get("hookSpecificOutput")
    if isinstance(hook_specific, dict) and hook_specific.get("permissionDecision") == "deny":
        return True
    return False


def extract_reason(result: dict[str, Any]) -> str:
    """Extract block reason from daemon response.

    Checks all reason fields used across CLIs in priority order:
    - stopReason (Claude, Gemini)
    - reason (all CLIs)
    """
    return (
        result.get("stopReason")
        or result.get("user_message")
        or result.get("reason")
        or "Blocked by hook"
    )


# ── Agent Daemon Failure Tracking ───────────────────────────────────────

_DAEMON_FAILURE_DIR = Path("/tmp/gobby-agent-failures")
_MAX_DAEMON_FAILURES = 5


def _track_daemon_failure(agent_run_id: str) -> int:
    """Track consecutive daemon-down failures for a spawned agent.

    Returns the current failure count after incrementing.
    """
    _DAEMON_FAILURE_DIR.mkdir(parents=True, exist_ok=True)
    failure_file = _DAEMON_FAILURE_DIR / agent_run_id
    count = 0
    if failure_file.exists():
        try:
            count = int(failure_file.read_text().strip())
        except (ValueError, OSError):
            pass
    count += 1
    try:
        failure_file.write_text(str(count))
    except OSError:
        pass
    return count


def _reset_daemon_failures(agent_run_id: str) -> None:
    """Reset the daemon failure counter on successful connection."""
    failure_file = _DAEMON_FAILURE_DIR / agent_run_id
    try:
        failure_file.unlink(missing_ok=True)
    except OSError:
        pass


def _force_kill_agent() -> None:
    """Force-kill the spawned agent process tree.

    Tries tmux kill-pane first (cleanest in tmux context), then
    falls back to killing the process group.
    """
    import subprocess as sp

    # Inside tmux: kill the pane (takes down the CLI and shell)
    if os.environ.get("TMUX"):
        try:
            sp.run(
                ["tmux", "kill-pane"],
                timeout=5,
                stdout=sp.DEVNULL,
                stderr=sp.DEVNULL,
            )
            return
        except Exception:
            pass

    # Fallback: kill the process group (all processes in the tmux pane
    # share a group under the shell leader)
    try:
        os.killpg(os.getpgid(os.getppid()), signal.SIGTERM)
    except Exception:
        # Last resort: kill direct parent
        try:
            os.kill(os.getppid(), signal.SIGTERM)
        except Exception:
            pass


# ── Daemon Health Check ─────────────────────────────────────────────────


async def check_daemon_running(timeout: float = 5.0) -> bool:
    """Check if gobby daemon is active and responding.

    Performs a health check to verify the HTTP server is running
    before processing hooks. This prevents hook execution when the daemon
    is stopped, avoiding long timeouts and confusing error messages.

    The 5s timeout is intentionally generous — healthy daemons respond in
    <10ms, so the timeout only fires on actual unreachability. A tight
    timeout (e.g. 0.5s) causes false positives during GC pauses or load
    spikes, which cascades into agent self-termination.

    Args:
        timeout: Maximum time to wait for response in seconds (default: 5.0)

    Returns:
        True if daemon is running and responding, False otherwise
    """
    try:
        import httpx

        daemon_url = await get_daemon_url()
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{daemon_url}/api/admin/health",
                timeout=timeout,
                follow_redirects=False,
            )
        return response.status_code == 200
    except Exception:
        return False


# ── Argument Parsing ────────────────────────────────────────────────────


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        Parsed arguments with type, cli, and debug flags
    """
    parser = argparse.ArgumentParser(description="Gobby Hook Dispatcher")
    parser.add_argument(
        "--type",
        required=True,
        help="Hook type (e.g., session-start, SessionStart, preToolUse)",
    )
    parser.add_argument(
        "--cli",
        default=None,
        help="CLI name (claude, gemini, qwen, codex)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


# ── Hook Logging ────────────────────────────────────────────────────────


def log_hook_details(
    logger: logging.Logger, hook_type: str, input_data: dict[str, Any], debug_mode: bool
) -> None:
    """Log hook-specific details.

    Uses normalized hook names to handle different naming conventions
    (kebab-case, PascalCase, camelCase, snake_case) across CLIs.
    """
    logger.info(f"[{hook_type}] Received input keys: {list(input_data.keys())}")

    session_id = input_data.get("session_id")

    # Normalize hook type for matching: strip hyphens, underscores, lowercase
    hook_norm = hook_type.lower().replace("-", "").replace("_", "")

    if hook_norm == "sessionstart":
        logger.info(f"[{hook_type}] session_id={session_id}, source={input_data.get('source')}")
    elif hook_norm == "sessionend":
        logger.info(f"[{hook_type}] session_id={session_id}, reason={input_data.get('reason')}")
    elif hook_norm in (
        "userpromptsubmit",
        "userpromptsubmitted",
        "beforeagent",
        "beforesubmitprompt",
        "preuserprompt",
    ):
        prompt = input_data.get("prompt", "")
        preview = prompt[:100] + "..." if len(prompt) > 100 else prompt
        logger.info(f"[{hook_type}] session_id={session_id}, prompt={preview}")
    elif hook_norm in (
        "pretooluse",
        "beforetool",
        "beforemcpexecution",
        "premcptooluse",
    ):
        tool_input = input_data.get("tool_input", {})
        if isinstance(tool_input, dict):
            tool_input_preview = {
                k: (v[:200] + "..." if isinstance(v, str) and len(v) > 200 else v)
                for k, v in tool_input.items()
            }
        else:
            tool_input_preview = tool_input
        tool_name = input_data.get("tool_name") or input_data.get("function_name", "unknown")
        logger.info(
            f"[{hook_type}] tool_name={tool_name}, "
            f"tool_input={tool_input_preview}, session_id={session_id}"
        )
    elif hook_norm in (
        "posttooluse",
        "aftertool",
        "aftermcpexecution",
        "postmcptooluse",
    ):
        tool_name = input_data.get("tool_name") or input_data.get("function_name", "unknown")
        logger.info(
            f"[{hook_type}] tool_name={tool_name}, "
            f"has_tool_response={bool(input_data.get('tool_response'))}, "
            f"session_id={session_id}"
        )
    elif hook_norm in ("precompact", "precompress"):
        logger.info(
            f"[{hook_type}] session_id={session_id}, "
            f"trigger={input_data.get('trigger')}, "
            f"has_custom_instructions={bool(input_data.get('custom_instructions'))}"
        )
    elif hook_norm == "stop":
        logger.info(
            f"[{hook_type}] session_id={session_id}, "
            f"stop_hook_active={input_data.get('stop_hook_active')}"
        )
    elif hook_norm in ("subagentstart", "subagentstop"):
        logger.info(
            f"[{hook_type}] session_id={session_id}, "
            f"agent_id={input_data.get('agent_id')}, "
            f"subagent_id={input_data.get('subagent_id')}"
        )
    elif hook_norm == "notification":
        logger.info(
            f"[{hook_type}] session_id={session_id}, "
            f"message={input_data.get('message')}, "
            f"title={input_data.get('title', 'N/A')}"
        )
    elif hook_norm == "permissionrequest":
        tool_input = input_data.get("tool_input", {})
        if isinstance(tool_input, dict):
            tool_input_preview = {
                k: (v[:200] + "..." if isinstance(v, str) and len(v) > 200 else v)
                for k, v in tool_input.items()
            }
        else:
            tool_input_preview = tool_input
        logger.info(
            f"[{hook_type}] tool_name={input_data.get('tool_name')}, "
            f"tool_input={tool_input_preview}, session_id={session_id}"
        )

    if debug_mode:
        logger.debug(f"Input data: {input_data}")


# ── Main ────────────────────────────────────────────────────────────────


def _find_project_config(cwd: str) -> dict[str, Any] | None:
    """Walk up from cwd to find .gobby/project.json and return parsed content.

    Uses stdlib only (no external deps). Returns None if not found or invalid.
    """
    current = Path(cwd)
    for _ in range(50):  # Safety limit to prevent infinite loops
        project_json = current / ".gobby" / "project.json"
        if project_json.is_file():
            try:
                with open(project_json) as f:
                    data: dict[str, Any] = json.load(f)
                    return data
            except (json.JSONDecodeError, OSError):
                return None
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


async def main() -> int:
    """Main dispatcher execution.

    Returns:
        Exit code (0=success/allow, 1=error, 2=block/deny)
    """
    # Check env var for hooks disabled (before any other work)
    if os.environ.get("GOBBY_HOOKS_DISABLED"):
        print(json.dumps({}))
        return 0

    # Check .gobby/project.json hooks_disabled flag
    project_config = _find_project_config(os.getcwd())
    if project_config and project_config.get("hooks_disabled"):
        print(json.dumps({}))
        return 0

    try:
        args = parse_arguments()
    except (argparse.ArgumentError, SystemExit):
        # Argument parsing failed - return empty dict
        # Exit 2 is used by 4/5 CLIs (only Gemini uses 1)
        print(json.dumps({}))
        return 2

    config = detect_cli(args)
    hook_type = args.type
    debug_mode = args.debug

    # Check if gobby daemon is running before processing hooks
    if not await check_daemon_running():
        # Spawned agents must stop immediately — without the daemon, hook
        # enforcement is unavailable and the agent is off-rails.
        agent_run_id = os.environ.get("GOBBY_AGENT_RUN_ID")
        if agent_run_id:
            failures = _track_daemon_failure(agent_run_id)
            if failures >= _MAX_DAEMON_FAILURES:
                print(
                    f"\nGobby daemon persistently unavailable "
                    f"({failures} consecutive hook failures). "
                    f"Force-terminating agent {agent_run_id}.",
                    file=sys.stderr,
                )
                _force_kill_agent()
                # If kill didn't work, fall through to block
            print(
                "\nGobby daemon is not running. "
                "Spawned agent tools are blocked — stop immediately. "
                f"(failure {failures}/{_MAX_DAEMON_FAILURES})",
                file=sys.stderr,
            )
            return 2

        if hook_type in config.critical_hooks:
            # Block the hook for interactive sessions on critical lifecycle events
            print(
                f"\nGobby daemon is not running. "
                f"({hook_type} requires daemon for session state management)",
                file=sys.stderr,
            )
            return 2
        else:
            # Non-critical hooks can proceed without daemon for interactive sessions
            print(
                json.dumps(
                    {"status": "daemon_not_running", "message": "gobby daemon is not running"}
                )
            )
            return 0

    # Daemon is running — reset failure counter for spawned agents
    agent_run_id = os.environ.get("GOBBY_AGENT_RUN_ID")
    if agent_run_id:
        _reset_daemon_failures(agent_run_id)

    # Setup logger for dispatcher
    logger = logging.getLogger(config.logger_name)
    if debug_mode:
        logging.basicConfig(level=logging.DEBUG)
    elif config.suppress_logs:
        # Suppress all logging to stderr - prevents polluting CLI stderr reading
        logging.basicConfig(level=logging.WARNING, handlers=[])
    else:
        logging.basicConfig(level=logging.INFO)

    try:
        # Read JSON input from stdin asynchronously
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(None, sys.stdin.read)
        input_data = json.loads(raw)

        # Inject terminal context for hooks that may need to register or
        # repair terminal-backed sessions later in the lifecycle.
        if hook_type in config.terminal_context_hooks and isinstance(input_data, dict):
            input_data.setdefault("terminal_context", get_terminal_context())

        log_hook_details(logger, hook_type, input_data, debug_mode)

    except json.JSONDecodeError as e:
        if debug_mode:
            logger.error(f"JSON decode error: {e}")
        print(json.dumps({}))
        return config.json_error_exit_code

    # Build project context headers so the daemon resolves the correct project.
    # The hook dispatcher runs in the CLI's project directory, so project_config
    # (from .gobby/project.json) is correct. The daemon's CWD is NOT.
    _context_headers: dict[str, str] = {}
    if project_config and project_config.get("id"):
        _context_headers["X-Gobby-Project-Id"] = str(project_config["id"])
    _hook_session_id = input_data.get("session_id") if isinstance(input_data, dict) else None
    if _hook_session_id:
        _context_headers["X-Gobby-Session-Id"] = str(_hook_session_id)

    if hook_type in _FIRE_AND_FORGET_HOOKS:
        import subprocess

        daemon_url = await get_daemon_url()
        payload = json.dumps(
            {
                "hook_type": hook_type,
                "input_data": input_data,
                "source": _detect_source(config),
            }
        )
        # Build curl header args for project context
        _curl_header_args: list[str] = []
        for hdr_name, hdr_val in _context_headers.items():
            _curl_header_args.extend(["-H", f"{hdr_name}: {hdr_val}"])
        try:
            logger.debug(
                f"Fire-and-forget hook {hook_type} → {daemon_url} ({len(payload)} bytes)",
            )
            proc = subprocess.Popen(
                [
                    "curl",
                    "-s",
                    "-X",
                    "POST",
                    f"{daemon_url}/api/hooks/execute",
                    "-H",
                    "Content-Type: application/json",
                    *_curl_header_args,
                    "-d",
                    "@-",
                    "--max-time",
                    "600",
                ],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.PIPE,
            )
            if proc.stdin is not None:
                proc.stdin.write(payload.encode())
                proc.stdin.close()
        except (FileNotFoundError, OSError) as e:
            logger.debug(f"Fire-and-forget spawn failed for {hook_type}: {e}")
        return 0

    # Call daemon HTTP endpoint
    import httpx

    daemon_url = await get_daemon_url()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{daemon_url}/api/hooks/execute",
                json={
                    "hook_type": hook_type,
                    "input_data": input_data,
                    "source": _detect_source(config),
                },
                headers=_context_headers,
                timeout=httpx.Timeout(10.0, read=600.0),
            )

        if response.status_code == 200:
            result = response.json()

            if debug_mode:
                logger.debug(f"Output data: {result}")

            # Check for block/deny decision
            if is_blocked(result):
                # Gemini and Codex read block decisions from JSON body for
                # tool hooks (non-zero exit codes are treated as "hook
                # failed", not "tool blocked"). Stop hooks are the
                # exception — they use exit code 2 to block, same as
                # Claude. Exit 0 would allow the stop.
                if config.source in ("gemini", "qwen", "codex") and hook_type != "Stop":
                    print(json.dumps(result))
                    return 0

                reason = extract_reason(result)
                print(f"\n{reason.rstrip()}", file=sys.stderr)
                return 2

            # Only print output if there's something meaningful to show
            # Empty dicts cause some CLIs to show "hook success: Success"
            if result:
                print(json.dumps(result))

            return 0
        else:
            # HTTP error from daemon
            error_detail = response.text
            logger.error(
                f"Daemon returned error: status={response.status_code}, detail={error_detail}"
            )
            # Fail closed for critical hooks (stop gates, session lifecycle) —
            # if the rule engine couldn't evaluate, block rather than allow
            if hook_type in config.critical_hooks:
                print(
                    f"\nHook error on critical hook '{hook_type}' — blocking to fail safe. "
                    f"Detail: {error_detail}",
                    file=sys.stderr,
                )
                return 2
            print(json.dumps({"status": "error", "message": f"Daemon error: {error_detail}"}))
            return 1

    except httpx.ConnectError:
        logger.error("Failed to connect to daemon (unreachable)")
        # Fail closed for critical hooks — daemon is reachable (passed check_daemon_running)
        # but connection failed, so enforcement state is unknown
        if hook_type in config.critical_hooks:
            print(
                f"\nDaemon connection failed on critical hook '{hook_type}' — blocking to fail safe.",
                file=sys.stderr,
            )
            return 2
        print(json.dumps({"status": "error", "message": "Daemon unreachable"}))
        return 1

    except httpx.TimeoutException:
        logger.error(f"Hook execution timeout: {hook_type}")
        # Fail closed for critical hooks — rule engine may have been evaluating a stop gate
        if hook_type in config.critical_hooks:
            print(
                f"\nHook timeout on critical hook '{hook_type}' — blocking to fail safe.",
                file=sys.stderr,
            )
            return 2
        print(json.dumps({"status": "error", "message": "Hook execution timeout"}))
        return 1

    except Exception as e:
        logger.error(f"Hook execution failed: {e}", exc_info=True)
        # Fail closed for critical hooks
        if hook_type in config.critical_hooks:
            print(
                f"\nHook failure on critical hook '{hook_type}' — blocking to fail safe. "
                f"Error: {e}",
                file=sys.stderr,
            )
            return 2
        print(json.dumps({"status": "error", "message": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
