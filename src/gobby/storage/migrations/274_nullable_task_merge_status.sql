ALTER TABLE tasks
ALTER COLUMN merge_in_progress DROP NOT NULL;

ALTER TABLE tasks
ALTER COLUMN blocked_by_merge DROP NOT NULL;
