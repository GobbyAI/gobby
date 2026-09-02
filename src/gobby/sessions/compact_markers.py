"""Leaf constants shared by compact continuation producers and consumers."""

HANDOFF_COMPACT_CONTINUE_VARIABLE = "handoff_compact_continue_pending"
COMPACT_NOTIFICATION_STARTED_AT_VARIABLE = "compact_notification_started_at"
COMPACT_HANDOFF_MARKER_VARIABLE = "handoff_source"
HANDOFF_COMPACT_CONTINUE_FRESH_SECONDS = 600
HANDOFF_COMPACT_CONTINUE_SEND_DELAY_SECONDS = 1.0
# A composer still settling a bracketed paste can swallow the Enter that
# follows it; a second Enter after this delay submits the trigger and is a
# no-op when the first Enter already submitted (empty composer).
HANDOFF_COMPACT_CONTINUE_SUBMIT_RETRY_DELAY_SECONDS = 1.5
