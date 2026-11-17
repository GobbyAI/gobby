-- Preserve run history while removing terminal identities no longer managed here.
UPDATE agent_runs SET terminal_id = NULL
WHERE terminal_id IN (
    SELECT id FROM terminals WHERE backend <> 'native' OR ownership <> 'gobby'
);

DELETE FROM terminals WHERE backend <> 'native' OR ownership <> 'gobby';

ALTER TABLE terminals
    DROP COLUMN session_name,
    DROP COLUMN window_id,
    DROP CONSTRAINT terminals_backend_check,
    DROP CONSTRAINT terminals_ownership_check,
    ADD CONSTRAINT terminals_backend_check CHECK (backend = 'native'),
    ADD CONSTRAINT terminals_ownership_check CHECK (ownership = 'gobby');
