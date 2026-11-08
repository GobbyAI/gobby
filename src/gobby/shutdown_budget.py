"""Shutdown time budget shared by the daemon and the CLI stop escalation.

The CLI sends SIGKILL this many seconds after SIGTERM. Every daemon shutdown budget
stacks, worst case, inside that window with ``SHUTDOWN_MARGIN_SECONDS`` to spare.
"""

from __future__ import annotations

DAEMON_STOP_ESCALATION_SECONDS = 32.0
SHUTDOWN_MARGIN_SECONDS = 5.0

# Child-process tail after an overrun, one bound per step. Each owning module reads its bound
# from here, so the tail sum is the sum of the bounds that actually run.
# Transcript pool: a bounded exit wait, then a kill whose reap is bounded too. An in-flight
# checkpoint write survives a kill because it is one atomic rename.
TRANSCRIPT_POOL_EXIT_SECONDS = 0.5
TRANSCRIPT_POOL_KILL_REAP_SECONDS = 0.5
# Internal CLI text generation calls: cancelled, then waited on for this long.
CLI_CALL_CANCEL_SECONDS = 1.0
# Managed-run listing that gates the reap. It starts at shutdown start, inside the graceful
# window, and the deadline path reads that result. Only a deadline-path re-list, needed when the
# start-time listing failed, spends AGENT_LISTING_SECONDS from the deadline sum below.
SHUTDOWN_AGENT_LISTING_SECONDS = 3.0
AGENT_LISTING_SECONDS = 1.0
# Reaper enumeration: two live-probe reads of at most 2s each (see srt_monitor).
CHILD_ENUMERATION_SECONDS = 4.0
# Each reaper sweep waits this long twice: after SIGTERM, then after SIGKILL.
CHILD_SWEEP_WAIT_SECONDS = 0.5
DEADLINE_CHILD_REAP_BUDGET_SECONDS = (
    TRANSCRIPT_POOL_EXIT_SECONDS
    + TRANSCRIPT_POOL_KILL_REAP_SECONDS
    + CLI_CALL_CANCEL_SECONDS
    + AGENT_LISTING_SECONDS
    + CHILD_ENUMERATION_SECONDS
    + 2 * CHILD_SWEEP_WAIT_SECONDS
)
