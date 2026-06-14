-- Allow validation_status='error' so an LLM infrastructure failure (no candidate
-- produced a usable result) is recorded distinctly from a genuine 'invalid'
-- verdict. This lets the dispatcher back off and retry instead of treating a
-- transient generation outage as a real validation rejection.
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_validation_status_check;
ALTER TABLE tasks
    ADD CONSTRAINT tasks_validation_status_check
    CHECK (validation_status IN ('pending', 'valid', 'invalid', 'error'));
