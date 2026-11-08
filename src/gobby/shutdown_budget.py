"""Shutdown time budget shared by the daemon and the CLI stop escalation.

The CLI sends SIGKILL this many seconds after SIGTERM. Every daemon shutdown budget
stacks, worst case, inside that window with ``SHUTDOWN_MARGIN_SECONDS`` to spare.
"""

from __future__ import annotations

DAEMON_STOP_ESCALATION_SECONDS = 32.0
SHUTDOWN_MARGIN_SECONDS = 5.0
