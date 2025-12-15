#!/usr/bin/env python3
"""Hook Dispatcher - Routes Claude Code hooks to HookManager.

This is a thin wrapper script that receives hook calls from Claude Code
and routes them to the appropriate handler via HookManager.

Usage:
    hook_dispatcher.py --type session-start < input.json > output.json
    hook_dispatcher.py --type pre-tool-use --debug < input.json > output.json

Exit Codes:
    0 - Success
    1 - General error (logged, continues)
    2 - Invalid input (argument parsing or JSON)
"""

import argparse
import json
import sys
from pathlib import Path

# No longer need to import HookManager - we call it via HTTP daemon instead

# Default daemon configuration
DEFAULT_DAEMON_PORT = 8765
DEFAULT_CONFIG_PATH = "~/.gobby/config.yaml"


def get_daemon_url() -> str:
    """Get the daemon HTTP URL from config file.

    Reads daemon_port from ~/.gobby/config.yaml if it exists,
    otherwise uses the default port 8765.

    Returns:
        Full daemon URL like http://localhost:8765
    """
    config_path = Path(DEFAULT_CONFIG_PATH).expanduser()

    if config_path.exists():
        try:
            import yaml

            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            port = config.get("daemon_port", DEFAULT_DAEMON_PORT)
        except Exception:
            # If config read fails, use default
            port = DEFAULT_DAEMON_PORT
    else:
        port = DEFAULT_DAEMON_PORT

    return f"http://localhost:{port}"


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        Parsed arguments with type and debug flags
    """
    parser = argparse.ArgumentParser(description="Claude Code Hook Dispatcher")
    parser.add_argument(
        "--type",
        required=True,
        help="Hook type (e.g., session-start, pre-tool-use)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def check_daemon_running(timeout: float = 0.5) -> bool:
    """Check if gobby daemon is active and responding.

    Performs a quick health check to verify the HTTP server is running
    before processing hooks. This prevents hook execution when the daemon
    is stopped, avoiding long timeouts and confusing error messages.

    Args:
        timeout: Maximum time to wait for response in seconds (default: 0.5)

    Returns:
        True if client is running and responding, False otherwise
    """
    try:
        import httpx

        daemon_url = get_daemon_url()
        response = httpx.get(
            f"{daemon_url}/admin/status",
            timeout=timeout,
            follow_redirects=False,
        )
        return response.status_code == 200
    except Exception:
        # Any error (connection refused, timeout, etc.) means client is not running
        return False


def main() -> int:
    """Main dispatcher execution.

    Returns:
        Exit code (0=success, 1=error, 2=invalid input)
    """
    try:
        # Parse arguments
        args = parse_arguments()
    except (argparse.ArgumentError, SystemExit):
        # Argument parsing failed - return empty dict and exit 2
        print(json.dumps({}))
        return 2

    hook_type = args.type
    debug_mode = args.debug

    # Check if gobby daemon is running before processing hooks
    if not check_daemon_running():
        # Daemon is not running - return gracefully without processing
        # Use status "daemon_not_running" to distinguish from errors
        print(
            json.dumps({"status": "daemon_not_running", "message": "gobby daemon is not running"})
        )
        return 0  # Exit 0 (success) - this is expected behavior, not an error

    # Setup logger for dispatcher (not HookManager)
    import logging

    logger = logging.getLogger("gobby.hooks.dispatcher")
    if debug_mode:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    try:
        # Read JSON input from stdin
        input_data = json.load(sys.stdin)

        # ALWAYS log what Claude Code sends us (for debugging hook data issues)
        logger.info(f"[{hook_type}] Received input keys: {list(input_data.keys())}")

        # Log hook-specific critical fields (based on Claude Code SDK documentation)
        if hook_type == "session-start":
            logger.info(
                f"[session-start] session_id={input_data.get('session_id')}, "
                f"source={input_data.get('source')}"
            )
        elif hook_type == "session-end":
            logger.info(
                f"[session-end] session_id={input_data.get('session_id')}, "
                f"reason={input_data.get('reason')}"
            )
        elif hook_type == "user-prompt-submit":
            prompt = input_data.get("prompt", "")
            prompt_preview = prompt[:100] + "..." if len(prompt) > 100 else prompt
            logger.info(
                f"[user-prompt-submit] session_id={input_data.get('session_id')}, "
                f"prompt={prompt_preview}"
            )
        elif hook_type == "pre-tool-use":
            tool_input = input_data.get("tool_input", {})
            # Truncate large values for readability (keep first 200 chars)
            tool_input_preview = {
                k: (v[:200] + "..." if isinstance(v, str) and len(v) > 200 else v)
                for k, v in tool_input.items()
            }
            logger.info(
                f"[pre-tool-use] tool_name={input_data.get('tool_name')}, "
                f"tool_input={tool_input_preview}, "
                f"session_id={input_data.get('session_id')}"
            )
        elif hook_type == "post-tool-use":
            logger.info(
                f"[post-tool-use] tool_name={input_data.get('tool_name')}, "
                f"has_tool_response={bool(input_data.get('tool_response'))}, "
                f"has_tool_input={bool(input_data.get('tool_input'))}, "
                f"session_id={input_data.get('session_id')}"
            )
        elif hook_type == "pre-compact":
            logger.info(
                f"[pre-compact] session_id={input_data.get('session_id')}, "
                f"trigger={input_data.get('trigger')}, "
                f"has_custom_instructions={bool(input_data.get('custom_instructions'))}"
            )
        elif hook_type == "stop":
            logger.info(
                f"[stop] session_id={input_data.get('session_id')}, "
                f"stop_hook_active={input_data.get('stop_hook_active')}"
            )
        elif hook_type == "subagent-start":
            logger.info(
                f"[subagent-start] session_id={input_data.get('session_id')}, "
                f"agent_id={input_data.get('agent_id')}, "
                f"subagent_id={input_data.get('subagent_id')}"
            )
        elif hook_type == "subagent-stop":
            logger.info(
                f"[subagent-stop] session_id={input_data.get('session_id')}, "
                f"agent_id={input_data.get('agent_id')}, "
                f"subagent_id={input_data.get('subagent_id')}"
            )
        elif hook_type == "notification":
            logger.info(
                f"[notification] session_id={input_data.get('session_id')}, "
                f"message={input_data.get('message')}, "
                f"title={input_data.get('title', 'N/A')}"
            )
        elif hook_type == "permission-request":
            tool_input = input_data.get("tool_input", {})
            tool_input_preview = {
                k: (v[:200] + "..." if isinstance(v, str) and len(v) > 200 else v)
                for k, v in tool_input.items()
            }
            logger.info(
                f"[permission-request] tool_name={input_data.get('tool_name')}, "
                f"tool_input={tool_input_preview}, "
                f"session_id={input_data.get('session_id')}"
            )

        if debug_mode:
            logger.debug(f"Input data: {input_data}")

    except json.JSONDecodeError as e:
        # Invalid JSON input - return empty dict and exit 2
        if debug_mode:
            logger.error(f"JSON decode error: {e}")
        print(json.dumps({}))
        return 2

    # Call daemon HTTP endpoint instead of creating HookManager
    import httpx

    daemon_url = get_daemon_url()
    try:
        response = httpx.post(
            f"{daemon_url}/hooks/execute",
            json={
                "hook_type": hook_type,
                "input_data": input_data,
                "source": "claude",  # Required: identifies CLI source
            },
            timeout=30.0,  # Generous timeout for hook processing
        )

        if response.status_code == 200:
            # Success - daemon returns result directly (not wrapped)
            result = response.json()

            if debug_mode:
                logger.debug(f"Output data: {result}")

            # Only print output if there's something meaningful to show
            # Empty dicts cause Claude Code to show "hook success: Success"
            if result and result != {}:
                print(json.dumps(result))
            # Otherwise print nothing - just exit 0
            return 0
        else:
            # HTTP error from daemon
            error_detail = response.text
            logger.error(
                f"Daemon returned error: status={response.status_code}, detail={error_detail}"
            )
            print(json.dumps({"status": "error", "message": f"Daemon error: {error_detail}"}))
            return 1

    except httpx.ConnectError:
        # Daemon not reachable - this shouldn't happen since we checked, but handle gracefully
        logger.error("Failed to connect to daemon (unreachable)")
        print(json.dumps({"status": "error", "message": "Daemon unreachable"}))
        return 1

    except httpx.TimeoutException:
        # Hook processing took too long
        logger.error(f"Hook execution timeout: {hook_type}")
        print(json.dumps({"status": "error", "message": "Hook execution timeout"}))
        return 1

    except Exception as e:
        # General error - log and return 1
        logger.error(f"Hook execution failed: {e}", exc_info=True)
        print(json.dumps({"status": "error", "message": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
