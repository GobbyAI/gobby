-- Idempotently relabel stale pre-removal Gemini session sources.
UPDATE sessions
SET source = 'unknown'
WHERE source = 'gemini';

UPDATE session_stop_signals
SET source = 'unknown'
WHERE source = 'gemini';
