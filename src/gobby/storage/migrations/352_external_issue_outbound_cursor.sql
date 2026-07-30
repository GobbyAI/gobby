ALTER TABLE external_issue_sync_status
    ADD COLUMN IF NOT EXISTS last_outbound_success_at TIMESTAMPTZ;
