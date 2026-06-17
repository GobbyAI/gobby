-- Clean legacy orphaned validation-backoff rows, then validate the FK added in 292.

DELETE FROM task_validation_backoff b
 WHERE NOT EXISTS (
       SELECT 1
         FROM tasks t
        WHERE t.id = b.task_id
 );

ALTER TABLE task_validation_backoff
    VALIDATE CONSTRAINT task_validation_backoff_task_id_fkey;
