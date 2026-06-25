UPDATE sessions
SET source = 'unknown', updated_at = NOW()
WHERE source = 'gemini';

UPDATE session_stop_signals
SET source = 'unknown'
WHERE source = 'gemini';
