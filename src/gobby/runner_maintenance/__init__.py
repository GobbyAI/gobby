"""Background maintenance tasks for GobbyRunner."""

import asyncio as asyncio

from gobby.cli.utils import get_gobby_home as get_gobby_home
from gobby.runner_maintenance.binaries import (
    _JITTER_RANDOM as _JITTER_RANDOM,
)
from gobby.runner_maintenance.binaries import (
    _sleep_until_next_bin_freshness_cycle as _sleep_until_next_bin_freshness_cycle,
)
from gobby.runner_maintenance.binaries import bin_freshness_loop as bin_freshness_loop
from gobby.runner_maintenance.isolation import (
    _ISOLATION_CLEANUP_SCAN_LIMIT as _ISOLATION_CLEANUP_SCAN_LIMIT,
)
from gobby.runner_maintenance.isolation import (
    _cleanup_missing_isolation_records as _cleanup_missing_isolation_records,
)
from gobby.runner_maintenance.isolation import (
    _cleanup_missing_isolation_records_async as _cleanup_missing_isolation_records_async,
)
from gobby.runner_maintenance.isolation import (
    _delete_missing_clone_records as _delete_missing_clone_records,
)
from gobby.runner_maintenance.isolation import (
    _delete_missing_worktree_records as _delete_missing_worktree_records,
)
from gobby.runner_maintenance.isolation import _run_git_command as _run_git_command
from gobby.runner_maintenance.isolation import (
    cleanup_expired_isolation_loop as cleanup_expired_isolation_loop,
)
from gobby.runner_maintenance.isolation import (
    tmux_window_name_repair_loop as tmux_window_name_repair_loop,
)
from gobby.runner_maintenance.lifecycle import cleanup_pid_file as cleanup_pid_file
from gobby.runner_maintenance.lifecycle import rebuild_vector_store as rebuild_vector_store
from gobby.runner_maintenance.lifecycle import setup_signal_handlers as setup_signal_handlers
from gobby.runner_maintenance.lifecycle import write_shutdown_source as write_shutdown_source
from gobby.runner_maintenance.messaging import (
    _COMMS_CLEANUP_BATCH_LIMIT as _COMMS_CLEANUP_BATCH_LIMIT,
)
from gobby.runner_maintenance.messaging import (
    cleanup_comms_messages_loop as cleanup_comms_messages_loop,
)
from gobby.runner_maintenance.messaging import (
    cleanup_zombie_messages_loop as cleanup_zombie_messages_loop,
)
from gobby.runner_maintenance.messaging import drain_hook_inbox_loop as drain_hook_inbox_loop
from gobby.runner_maintenance.storage_hygiene import (
    _APPROVAL_EXPIRY_BATCH_LIMIT as _APPROVAL_EXPIRY_BATCH_LIMIT,
)
from gobby.runner_maintenance.storage_hygiene import (
    _CHAT_ATTACHMENT_CLEANUP_BATCH_LIMIT as _CHAT_ATTACHMENT_CLEANUP_BATCH_LIMIT,
)
from gobby.runner_maintenance.storage_hygiene import (
    _SKILL_CLEANUP_BATCH_LIMIT as _SKILL_CLEANUP_BATCH_LIMIT,
)
from gobby.runner_maintenance.storage_hygiene import (
    _remove_stale_chat_attachment_file as _remove_stale_chat_attachment_file,
)
from gobby.runner_maintenance.storage_hygiene import (
    cleanup_chat_attachments_loop as cleanup_chat_attachments_loop,
)
from gobby.runner_maintenance.storage_hygiene import (
    expire_approval_timeouts_loop as expire_approval_timeouts_loop,
)
from gobby.runner_maintenance.storage_hygiene import (
    purge_deleted_skills_loop as purge_deleted_skills_loop,
)
from gobby.runner_maintenance.storage_hygiene import (
    sweep_test_schemas_loop as sweep_test_schemas_loop,
)
from gobby.runner_maintenance.telemetry_loops import (
    _METRIC_SNAPSHOT_CLEANUP_BATCH_LIMIT as _METRIC_SNAPSHOT_CLEANUP_BATCH_LIMIT,
)
from gobby.runner_maintenance.telemetry_loops import (
    loop_progress_cleanup_loop as loop_progress_cleanup_loop,
)
from gobby.runner_maintenance.telemetry_loops import (
    metric_snapshot_loop as metric_snapshot_loop,
)
from gobby.runner_maintenance.telemetry_loops import (
    recall_drift_monitor_loop as recall_drift_monitor_loop,
)
from gobby.runner_maintenance.telemetry_loops import span_cleanup_loop as span_cleanup_loop
from gobby.runner_maintenance.telemetry_loops import (
    unmodeled_observation_cleanup_loop as unmodeled_observation_cleanup_loop,
)
from gobby.runner_maintenance_recurring import (
    _wait_for_first_maintenance_cycle as _wait_for_first_maintenance_cycle,
)
from gobby.runner_maintenance_recurring import (
    memory_reconcile_loop as memory_reconcile_loop,
)
from gobby.runner_maintenance_recurring import (
    metrics_archive_loop as metrics_archive_loop,
)
from gobby.runner_maintenance_recurring import (
    metrics_cleanup_loop as metrics_cleanup_loop,
)
from gobby.runner_tmux_repair import (
    _select_tmux_repair_sessions as _select_tmux_repair_sessions,
)
from gobby.runner_tmux_repair import (
    _tmux_repair_candidate_score as _tmux_repair_candidate_score,
)
from gobby.runner_tmux_repair import (
    _tmux_repair_pane_key as _tmux_repair_pane_key,
)
