ALTER TABLE worktrees
ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ;

UPDATE worktrees
SET last_activity_at = updated_at
WHERE last_activity_at IS NULL;
