ALTER TABLE cron_jobs
    ADD COLUMN IF NOT EXISTS display_name TEXT;
