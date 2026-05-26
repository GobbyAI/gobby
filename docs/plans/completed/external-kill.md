# External Kill Strategy for Agent Termination

**Status:** COMPLETED

**Implementation locations:**
- `src/gobby/agents/registry.py` - `kill()` method
- `src/gobby/mcp_proxy/tools/agents.py` - `kill_agent` MCP tool
- `src/gobby/cli/agents.py` - `gobby agents kill` CLI command

---

## Overview

Add process termination for spawned agents. Currently `cancel_agent` only updates DB status - processes continue running. This adds real process killing with mode-aware strategies.

## Problem Analysis

| Mode | PID Captured | Can Kill? | Strategy |
|------|-------------|-----------|----------|
| **headless** | Actual CLI PID | Yes | Direct SIGTERM |
| **terminal** (macOS) | `open` PID (useless!) | No | Use session DB or pgrep |
| **terminal** (Linux) | Actual terminal PID | Partial | SIGTERM terminal OR pgrep inner |
| **in_process** | None (asyncio.Task) | Yes | Task.cancel() |

## PID Capture by Provider

| Provider | Hook System | PID Source |
|----------|-------------|------------|
| **Claude** | Shell hooks (`hook_dispatcher.py`) | `session.terminal_context["parent_pid"]` |
| **Codex** | JSON-RPC events (internal) | Use pgrep fallback |
| **Gemini** | None yet (coming soon) | Use pgrep fallback |

**Strategy by provider:**

1. **Claude**: Session hook captures `os.getppid()` → stored in DB → use directly
2. **Codex/Gemini**: Fall back to `pgrep -f "Your Gobby session_id is: {id}"`

## Platform Considerations

| Platform | Signal | Process Finder | Notes |
|----------|--------|----------------|-------|
| **macOS** | `os.kill(SIGTERM)` | `pgrep -f` | `open` exits immediately |
| **Linux** | `os.kill(SIGTERM)` | `pgrep -f` | Terminal PID may be usable |
| **Windows** | `subprocess.terminate()` | `tasklist /FI` | Uses `TerminateProcess` |

**Windows implementation:**

```python
if platform.system() == "Windows":
    # No os.kill with signals on Windows
    import subprocess
    subprocess.run(["taskkill", "/F", "/PID", str(target_pid)], check=True)
else:
    os.kill(target_pid, signal.SIGTERM)
```

**Windows pgrep equivalent:**

```python
# Find process by command line pattern
result = subprocess.run(
    ["wmic", "process", "where",
     f"CommandLine like '%Your Gobby session_id is: {session_id}%'",
     "get", "ProcessId"],
    capture_output=True, text=True
)
```

**Security Note:** The `session_id` embedded in WMIC/pgrep queries must be validated before use to prevent command injection. Gobby session IDs are UUIDs, so validation should:

1. **Validate format**: Ensure session_id matches UUID pattern (`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
2. **Escape special characters**: If non-UUID formats are ever supported, escape quotes (`"`), percent signs (`%`), ampersands (`&`), and other shell/WMIC metacharacters
3. **Reject invalid input**: Never pass unvalidated user input to subprocess commands

The current implementation in `registry.py` uses session IDs from the database (already validated on creation), but any future changes should maintain this validation.

## Implementation

### Files Modified

| File | Changes |
|------|---------|
| `src/gobby/agents/registry.py` | Add `kill()` method with mode-aware strategies |
| `src/gobby/mcp_proxy/tools/agents.py` | Add `kill_agent` MCP tool |
| `src/gobby/cli/agents.py` | Add `gobby agents kill` CLI command |

### 1. Registry Method (`registry.py`)

Add to `RunningAgentRegistry` class:

```python
def kill(
    self,
    run_id: str,
    signal_name: str = "TERM",
    timeout: float = 5.0,
) -> dict[str, Any]:
    """
    Kill a running agent process.

    Strategy varies by mode:
    - headless: Direct signal to tracked PID
    - terminal: pgrep by session_id to find inner CLI process
    - in_process: Cancel asyncio task

    Platform-aware:
    - POSIX: Uses os.kill with signals (SIGTERM, SIGKILL)
    - Windows: Uses taskkill command

    Args:
        run_id: Agent run ID
        signal_name: Signal without SIG prefix (TERM, KILL, INT)
        timeout: Seconds before escalating TERM → KILL

    Returns:
        Dict with success status and details
    """
    import os
    import platform
    import subprocess
    import sys

    # Platform detection
    is_windows = os.name == "nt" or sys.platform == "win32"

    # Import signal only on POSIX (not available on Windows in same way)
    if not is_windows:
        import signal

    agent = self.get(run_id)
    if not agent:
        return {"success": False, "error": "Agent not found in registry"}

    # Handle in_process mode (asyncio.Task)
    if agent.mode == "in_process" and agent.task:
        agent.task.cancel()
        self.remove(run_id, status="cancelled")
        return {"success": True, "message": "Cancelled in-process task"}

    # For terminal mode, find PID via multiple strategies
    target_pid = agent.pid
    if agent.mode == "terminal" and agent.session_id:
        # Strategy 1: Check session's terminal_context (Claude captures PID via hooks)
        from gobby.storage.sessions import LocalSessionManager
        from gobby.storage.database import HubDatabase
        try:
            db = HubDatabase()
            session_mgr = LocalSessionManager(db)
            session = session_mgr.get_session(agent.session_id)
            if session and session.terminal_context:
                ctx_pid = session.terminal_context.get("parent_pid")
                if ctx_pid:
                    target_pid = int(ctx_pid)
                    self._logger.info(f"Found PID from session terminal_context: {target_pid}")
        except Exception as e:
            self._logger.debug(f"terminal_context lookup failed: {e}")

        # Strategy 2: Platform-specific process finder fallback
        if target_pid == agent.pid or not target_pid:
            try:
                if is_windows:
                    # Windows: Use WMIC to find process by command line
                    result = subprocess.run(
                        [
                            "wmic", "process", "where",
                            f"CommandLine like '%Your Gobby session_id is: {agent.session_id}%'",
                            "get", "ProcessId"
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5.0,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        # WMIC output has header line, parse PIDs from remaining lines
                        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
                        if len(lines) > 1:  # Skip header
                            for line in lines[1:]:
                                if line.isdigit():
                                    target_pid = int(line)
                                    self._logger.info(f"Found PID via WMIC: {target_pid}")
                                    break
                else:
                    # POSIX: Use pgrep
                    result = subprocess.run(
                        ["pgrep", "-f", f"Your Gobby session_id is: {agent.session_id}"],
                        capture_output=True,
                        text=True,
                        timeout=5.0,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        pids = result.stdout.strip().split("\n")
                        target_pid = int(pids[0])
                        self._logger.info(f"Found PID via pgrep: {target_pid}")
            except Exception as e:
                self._logger.warning(f"Process finder fallback failed: {e}")

    if not target_pid:
        return {"success": False, "error": "No target PID found"}

    # Platform-specific process aliveness check
    def is_process_alive(pid: int) -> bool | None:
        """Check if process is alive. Returns True/False, or None on permission error."""
        if is_windows:
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
                return str(pid) in result.stdout
            except Exception:
                return None
        else:
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return None

    # Check if process is alive
    alive_status = is_process_alive(target_pid)
    if alive_status is False:
        self.remove(run_id, status="completed")
        return {"success": True, "message": f"Process {target_pid} already dead", "already_dead": True}
    elif alive_status is None:
        return {"success": False, "error": f"No permission to check PID {target_pid}"}

    # Close PTY if embedded mode (POSIX only)
    if agent.master_fd is not None and not is_windows:
        try:
            os.close(agent.master_fd)
        except OSError:
            pass

    # Platform-specific process termination
    def terminate_process(pid: int, force: bool = False) -> bool:
        """Terminate process. Returns True if signal sent successfully."""
        if is_windows:
            try:
                # /T = terminate child processes, /F = force
                cmd = ["taskkill", "/PID", str(pid), "/T"]
                if force:
                    cmd.append("/F")
                result = subprocess.run(cmd, capture_output=True, timeout=5.0)
                return result.returncode == 0
            except Exception:
                return False
        else:
            try:
                sig = signal.SIGKILL if force else getattr(signal, f"SIG{signal_name}", signal.SIGTERM)
                os.kill(pid, sig)
                return True
            except ProcessLookupError:
                return False

    # Send initial termination signal
    force_kill = signal_name == "KILL"
    if not terminate_process(target_pid, force=force_kill):
        # Process may have died during termination attempt
        if is_process_alive(target_pid) is False:
            self.remove(run_id, status="completed")
            return {"success": True, "message": "Process died during signal", "already_dead": True}

    # Wait for termination with optional force escalation
    if signal_name == "TERM" and timeout > 0:
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if is_process_alive(target_pid) is False:
                break
            time.sleep(0.1)
        else:
            # Still alive - escalate to force kill
            terminate_process(target_pid, force=True)
            self._logger.info(f"Escalated to force kill for PID {target_pid}")

    self.remove(run_id, status="killed")
    return {
        "success": True,
        "message": f"Sent {'force kill' if force_kill else signal_name} to PID {target_pid}",
        "pid": target_pid,
        "signal": signal_name,
        "found_via": "wmic" if is_windows and target_pid != agent.pid else (
            "pgrep" if target_pid != agent.pid else "registry"
        ),
    }
```

### 2. MCP Tool (`agents.py` ~line 900)

```python
@registry.tool(
    name="kill_agent",
    description="Kill a running agent process by sending a signal (SIGTERM/SIGKILL).",
)
async def kill_agent(
    run_id: str,
    signal: str = "TERM",
    force: bool = False,
) -> dict[str, Any]:
    """
    Kill a running agent process.

    Args:
        run_id: The agent run ID to kill.
        signal: Signal to send (TERM, KILL, INT). Default: TERM.
        force: If True, use SIGKILL immediately.

    Returns:
        Dict with success status and kill details.
    """
    if force:
        signal = "KILL"

    result = agent_registry.kill(run_id, signal_name=signal)

    if result.get("success"):
        runner.cancel_run(run_id)  # Update database

    return result
```

### 3. CLI Command (`cli/agents.py` ~line 380)

```python
@agents.command("kill")
@click.argument("run_ref")
@click.option("--signal", "-s", default="TERM", help="Signal to send (TERM, KILL, INT)")
@click.option("--force", "-f", is_flag=True, help="Use SIGKILL immediately")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def kill_agent_cmd(run_ref: str, signal: str, force: bool, yes: bool) -> None:
    """Kill a running agent process (UUID or prefix).

    Sends SIGTERM by default. For terminal agents, finds the inner CLI
    process via pgrep and kills it.

    Examples:
        gobby agents kill abc123
        gobby agents kill abc123 --force
        gobby agents kill abc123 -s KILL
    """
    run_id = resolve_agent_run_id(run_ref)

    if not yes:
        click.confirm(f"Kill agent {run_id}?", abort=True)

    daemon_url = get_daemon_url()

    try:
        response = httpx.post(
            f"{daemon_url}/mcp/gobby-agents/tools/kill_agent",
            json={"run_id": run_id, "signal": signal, "force": force},
            timeout=15.0,
        )
        response.raise_for_status()
        result = response.json()
    except httpx.ConnectError:
        click.echo("Error: Cannot connect to Gobby daemon", err=True)
        return
    except httpx.HTTPStatusError as e:
        click.echo(f"Error: {e.response.status_code}", err=True)
        return

    if result.get("success"):
        click.echo(result.get("message", f"Killed agent {run_id}"))
        if result.get("found_via") == "pgrep":
            click.echo(f"  (found via pgrep, PID {result.get('pid')})")
        if result.get("already_dead"):
            click.echo("  (process was already terminated)")
    else:
        click.echo(f"Failed: {result.get('error')}", err=True)
```

## Behavior Summary

| Mode | Kill Strategy |
|------|---------------|
| `headless` | Direct SIGTERM to tracked PID → SIGKILL if needed |
| `terminal` | pgrep for session_id in command → kill inner process |
| `in_process` | asyncio.Task.cancel() |
| `embedded` | Close PTY fd + signal |

## Terminal Close Behavior

When the inner process (claude/gemini/codex) is killed:

- Terminal may close automatically (if configured)
- Terminal may show "[Process exited]" and stay open
- Terminal will NOT prompt "Are you sure?" because WE initiated the kill

## Verification

```bash
# 1. Start a terminal agent
gobby agents start "wait for 60 seconds" -s $(gobby sessions list --json | jq -r '.[0].id')

# 2. List to get run_id
gobby agents list --status running

# 3. Kill it
gobby agents kill <run-id> -y

# 4. Verify
gobby agents show <run-id>  # Status: killed
pgrep -f "wait for 60"      # Should return nothing
```

---

## Related

- Task: #3363 (Add kill_agent functionality)
