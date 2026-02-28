"""
Daemon watchdog process.

Monitors daemon health via HTTP and restarts it if unresponsive.
Runs as a separate process that survives daemon crashes.

Usage:
    python -m gobby.watchdog --port 60887 [--verbose]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess  # nosec B404 - subprocess needed for daemon restart
import sys
import time
from collections import deque
from pathlib import Path

import httpx

from gobby.config.app import load_config
from gobby.config.watchdog import WatchdogConfig

logger = logging.getLogger(__name__)


def get_gobby_home() -> Path:
    """Get gobby home directory, respecting GOBBY_HOME env var."""
    gobby_home = os.environ.get("GOBBY_HOME")
    if gobby_home:
        return Path(gobby_home)
    return Path.home() / ".gobby"


class Watchdog:
    """
    Daemon watchdog that monitors health and restarts on failure.

    Features:
    - HTTP health checks to /api/admin/health
    - Consecutive failure threshold before restart
    - Circuit breaker to prevent restart loops
    - Graceful shutdown handling
    """

    def __init__(
        self,
        daemon_port: int,
        config: WatchdogConfig | None = None,
        verbose: bool = False,
    ):
        self.daemon_port = daemon_port
        self.config = config or WatchdogConfig()
        self.verbose = verbose

        # State tracking
        self.consecutive_failures = 0
        self.restart_times: deque[float] = deque(maxlen=self.config.max_restarts_per_hour)
        self.last_restart_time: float = 0
        self.running = True
        self._state_file = get_gobby_home() / "watchdog_state.json"

        # Load persisted circuit breaker state so it survives watchdog restarts
        self._load_state()

        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

    def _load_state(self) -> None:
        """Load persisted circuit breaker state from disk."""
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text())
                hour_ago = time.time() - 3600
                for t in data.get("restart_times", []):
                    if t > hour_ago:
                        self.restart_times.append(t)
                if self.restart_times:
                    self.last_restart_time = self.restart_times[-1]
                    logger.info(
                        f"Loaded {len(self.restart_times)} recent restart(s) from state file"
                    )
        except Exception as e:
            logger.warning(f"Failed to load watchdog state: {e}")

    def _save_state(self) -> None:
        """Persist circuit breaker state to disk."""
        try:
            data = {"restart_times": list(self.restart_times)}
            self._state_file.write_text(json.dumps(data))
        except Exception as e:
            logger.warning(f"Failed to save watchdog state: {e}")

    def _handle_shutdown(self, signum: int, frame: object) -> None:
        """Handle shutdown signals gracefully."""
        logger.info(f"Received signal {signum}, shutting down watchdog")
        self.running = False

    def check_health(self) -> bool:
        """
        Check daemon health via HTTP endpoint.

        Returns:
            True if daemon is healthy, False otherwise.
        """
        try:
            response = httpx.get(
                f"http://localhost:{self.daemon_port}/api/admin/health",
                timeout=5.0,
            )
            if response.status_code == 200:
                if self.verbose:
                    logger.debug("Health check passed")
                return True
            else:
                logger.warning(f"Health check returned status {response.status_code}")
                return False
        except httpx.ConnectError:
            logger.warning("Health check failed: connection refused")
            return False
        except httpx.TimeoutException:
            logger.warning("Health check failed: timeout")
            return False
        except Exception as e:
            logger.warning(f"Health check failed: {e}")
            return False

    def _is_daemon_running(self) -> bool:
        """Check if daemon process is running via PID file."""
        pid_file = get_gobby_home() / "gobby.pid"
        if not pid_file.exists():
            return False

        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # Check if process exists
            return True
        except (ProcessLookupError, ValueError, OSError):
            return False

    def _circuit_breaker_triggered(self) -> bool:
        """
        Check if circuit breaker should prevent restart.

        Returns:
            True if too many restarts have occurred in the last hour.
        """
        now = time.time()
        hour_ago = now - 3600

        # Count restarts in the last hour
        recent_restarts = sum(1 for t in self.restart_times if t > hour_ago)

        if recent_restarts >= self.config.max_restarts_per_hour:
            logger.error(
                f"Circuit breaker triggered: {recent_restarts} restarts in the last hour "
                f"(max: {self.config.max_restarts_per_hour})"
            )
            return True
        return False

    def _cooldown_active(self) -> bool:
        """Check if restart cooldown is still active."""
        if self.last_restart_time == 0:
            return False

        elapsed = time.time() - self.last_restart_time
        if elapsed < self.config.restart_cooldown:
            remaining = self.config.restart_cooldown - elapsed
            logger.debug(f"Restart cooldown active, {remaining:.1f}s remaining")
            return True
        return False

    def should_restart(self) -> bool:
        """
        Determine if daemon should be restarted.

        Returns:
            True if restart should be attempted.
        """
        # Check failure threshold
        if self.consecutive_failures < self.config.failure_threshold:
            return False

        # Check cooldown
        if self._cooldown_active():
            return False

        # Check circuit breaker
        if self._circuit_breaker_triggered():
            return False

        return True

    def restart_daemon(self) -> bool:
        """
        Restart the daemon process.

        Returns:
            True if restart was successful.
        """
        logger.info("Attempting to restart daemon...")

        gobby_dir = get_gobby_home()
        pid_file = gobby_dir / "gobby.pid"

        # Stop existing daemon if running
        if pid_file.exists():
            try:
                with open(pid_file) as f:
                    pid = int(f.read().strip())

                logger.info(f"Stopping existing daemon (PID {pid})")
                os.kill(pid, signal.SIGTERM)

                # Wait for graceful shutdown
                for _ in range(100):  # 10 seconds max
                    time.sleep(0.1)
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                else:
                    # Force kill if still running
                    try:
                        os.kill(pid, signal.SIGKILL)
                        time.sleep(0.5)
                    except ProcessLookupError:
                        pass

                pid_file.unlink(missing_ok=True)

            except (ProcessLookupError, ValueError):
                pid_file.unlink(missing_ok=True)
            except Exception as e:
                logger.error(f"Error stopping daemon: {e}")

        # Wait for port to be released
        time.sleep(2.0)

        # Start new daemon
        try:
            config = load_config(create_default=False)
            log_file = Path(config.logging.client).expanduser()
            error_log_file = Path(config.logging.client_error).expanduser()

            cmd = [sys.executable, "-m", "gobby.runner"]
            if self.verbose:
                cmd.append("--verbose")

            log_f = open(log_file, "a")
            error_log_f = open(error_log_file, "a")

            try:
                process = subprocess.Popen(  # nosec B603
                    cmd,
                    stdout=log_f,
                    stderr=error_log_f,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    env=os.environ.copy(),
                )

                # Write new PID file
                with open(pid_file, "w") as f:
                    f.write(str(process.pid))

                logger.info(f"Started new daemon (PID {process.pid})")

            finally:
                log_f.close()
                error_log_f.close()

            # Wait for daemon to become healthy.
            # Startup involves sync tasks (skills, prompts, workflows, agents,
            # task import) that can take 30-45s before the HTTP server binds.
            time.sleep(5.0)

            # Verify daemon is responding (up to 55s total: 5s + 25 * 2s)
            for i in range(25):
                if self.check_health():
                    logger.info("Daemon restart successful")
                    self.last_restart_time = time.time()
                    self.restart_times.append(self.last_restart_time)
                    self._save_state()
                    self.consecutive_failures = 0
                    return True
                if i % 5 == 4:
                    logger.info(f"Waiting for daemon startup... ({(i + 1) * 2}s elapsed)")
                time.sleep(2.0)

            logger.error("Daemon started but not responding to health checks after 55s")
            return False

        except Exception as e:
            logger.error(f"Failed to restart daemon: {e}")
            return False

    def run(self) -> None:
        """Main watchdog loop."""
        logger.info(
            f"Watchdog starting: port={self.daemon_port}, "
            f"interval={self.config.health_check_interval}s, "
            f"threshold={self.config.failure_threshold}"
        )

        # Write our own PID file
        watchdog_pid_file = get_gobby_home() / "watchdog.pid"
        with open(watchdog_pid_file, "w") as f:
            f.write(str(os.getpid()))

        try:
            while self.running:
                # Perform health check
                if self.check_health():
                    self.consecutive_failures = 0
                else:
                    self.consecutive_failures += 1
                    logger.warning(
                        f"Health check failed ({self.consecutive_failures}/"
                        f"{self.config.failure_threshold})"
                    )

                    # Check if we should restart
                    if self.should_restart():
                        logger.warning(
                            f"Failure threshold reached after {self.consecutive_failures} "
                            "consecutive failures"
                        )
                        if not self.restart_daemon():
                            logger.error("Restart attempt failed")

                # Sleep until next check
                # Use short intervals for responsive shutdown
                sleep_remaining = self.config.health_check_interval
                while sleep_remaining > 0 and self.running:
                    sleep_time = min(1.0, sleep_remaining)
                    time.sleep(sleep_time)
                    sleep_remaining -= sleep_time

        finally:
            # Clean up PID file
            watchdog_pid_file.unlink(missing_ok=True)
            logger.info("Watchdog stopped")


def main() -> None:
    """Entry point for watchdog process."""
    parser = argparse.ArgumentParser(description="Gobby daemon watchdog")
    parser.add_argument(
        "--port",
        type=int,
        required=True,
        help="Daemon HTTP port to monitor",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO

    # Load config to get log file path
    try:
        config = load_config(create_default=False)
        log_file = Path(config.logging.watchdog).expanduser()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        watchdog_config = config.watchdog
    except Exception:
        log_file = get_gobby_home() / "logs" / "watchdog.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        watchdog_config = WatchdogConfig()

    # Configure logging to file
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler() if args.verbose else logging.NullHandler(),
        ],
    )

    # Run watchdog
    watchdog = Watchdog(
        daemon_port=args.port,
        config=watchdog_config,
        verbose=args.verbose,
    )
    watchdog.run()


if __name__ == "__main__":
    main()
