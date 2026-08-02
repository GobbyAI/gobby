UPDATE tasks
SET task_type = 'task'
WHERE task_type = 'review_anchor';

DELETE FROM task_type_default_stages
WHERE task_type = 'review_anchor';
