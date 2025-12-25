#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx",
#     "pyyaml",
# ]
# ///
"""Hook Dispatcher - Routes Gemini CLI hooks to HookManager.

This is a thin wrapper script that receives hook calls from Gemini CLI
and routes them to the appropriate handler via HookManager.

Gemini CLI invokes hooks with JSON input on stdin and expects JSON output
on stdout. Exit codes: 0 = allow, 2 = deny.

Usage:
    hook_dispatcher.py --type SessionStart < input.json > output.json
    hook_dispatcher.py --type BeforeTool --debug < input.json > output.json

Exit Codes:
    0 - Success / Allow
    1 - General error (logged, continues)
    2 - Deny / Block
"""

import argparse
import json
import sys
from pathlib import Path

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
    parser = argparse.ArgumentParser(description="Gemini CLI Hook Dispatcher")
    parser.add_argument(
        "--type",
        required=True,
        help="Hook type (e.g., SessionStart, BeforeTool)",
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
        True if daemon is running and responding, False otherwise
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
        Exit code (0=allow, 1=error, 2=deny)
    """
    try:
        # Parse arguments
        args = parse_arguments()
    except (argparse.ArgumentError, SystemExit):
        # Argument parsing failed - return empty dict and exit 1
        print(json.dumps({}))
        return 1

    hook_type = args.type  # PascalCase: SessionStart, BeforeTool, etc.
    debug_mode = args.debug

    # Check if gobby daemon is running before processing hooks
    if not check_daemon_running():
        # Daemon is not running - return gracefully without processing
        print(
            json.dumps({"status": "daemon_not_running", "message": "gobby daemon is not running"})
        )
        return 0  # Exit 0 (allow) - this is expected behavior, not an error

    # Setup logger for dispatcher (not HookManager)
    import logging

    logger = logging.getLogger("gobby.hooks.gemini.dispatcher")
    if debug_mode:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    try:
        # Read JSON input from stdin
        input_data = json.load(sys.stdin)

        # Log what Gemini CLI sends us (for debugging hook data issues)
        logger.info(f"[{hook_type}] Received input keys: {list(input_data.keys())}")

        # Log hook-specific critical fields
        if hook_type == "SessionStart":
            logger.info(f"[SessionStart] session_id={input_data.get('session_id')}")
        elif hook_type == "SessionEnd":
            logger.info(
                f"[SessionEnd] session_id={input_data.get('session_id')}, "
                f"reason={input_data.get('reason')}"
            )
        elif hook_type == "BeforeAgent":
            prompt = input_data.get("prompt", "")
            prompt_preview = prompt[:100] + "..." if len(prompt) > 100 else prompt
            logger.info(
                f"[BeforeAgent] session_id={input_data.get('session_id')}, prompt={prompt_preview}"
            )
        elif hook_type == "BeforeTool":
            tool_name = input_data.get("tool_name") or input_data.get("function_name", "unknown")
            logger.info(
                f"[BeforeTool] tool_name={tool_name}, session_id={input_data.get('session_id')}"
            )
        elif hook_type == "AfterTool":
            tool_name = input_data.get("tool_name") or input_data.get("function_name", "unknown")
            logger.info(
                f"[AfterTool] tool_name={tool_name}, session_id={input_data.get('session_id')}"
            )
        elif hook_type == "BeforeToolSelection":
            logger.info(f"[BeforeToolSelection] session_id={input_data.get('session_id')}")
        elif hook_type == "BeforeModel":
            logger.info(
                f"[BeforeModel] session_id={input_data.get('session_id')}, "
                f"model={input_data.get('model', 'unknown')}"
            )
        elif hook_type == "AfterModel":
            logger.info(f"[AfterModel] session_id={input_data.get('session_id')}")
        elif hook_type == "PreCompress":
            logger.info(f"[PreCompress] session_id={input_data.get('session_id')}")
        elif hook_type == "Notification":
            logger.info(
                f"[Notification] session_id={input_data.get('session_id')}, "
                f"message={input_data.get('message')}"
            )
        elif hook_type == "AfterAgent":
            logger.info(f"[AfterAgent] session_id={input_data.get('session_id')}")

        if debug_mode:
            logger.debug(f"Input data: {input_data}")

    except json.JSONDecodeError as e:
        # Invalid JSON input - return empty dict and exit 1
        if debug_mode:
            logger.error(f"JSON decode error: {e}")
        print(json.dumps({}))
        return 1

    # Call daemon HTTP endpoint
    import httpx

    daemon_url = get_daemon_url()
    try:
        response = httpx.post(
            f"{daemon_url}/hooks/execute",
            json={
                "hook_type": hook_type,  # PascalCase for Gemini
                "input_data": input_data,
                "source": "gemini",  # Required: identifies CLI source
            },
            timeout=30.0,  # Generous timeout for hook processing
        )

        if response.status_code == 200:
            # Success - daemon returns result directly
            result = response.json()

            if debug_mode:
                logger.debug(f"Output data: {result}")

            # Determine exit code based on decision
            decision = result.get("decision", "allow")

            # Print JSON output for Gemini CLI
            if result and result != {}:
                print(json.dumps(result))

            # Exit code: 0 = allow, 2 = deny
            if decision == "deny":
                return 2
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
        # Daemon not reachable
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
