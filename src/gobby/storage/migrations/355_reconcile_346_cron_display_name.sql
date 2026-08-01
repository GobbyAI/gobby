-- gobby:destructive
ALTER TABLE cron_jobs
    ADD COLUMN IF NOT EXISTS display_name TEXT;

DROP TABLE IF EXISTS tmux_input_requests, tmux_input_pane_states;
