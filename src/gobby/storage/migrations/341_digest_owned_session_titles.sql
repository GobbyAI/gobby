UPDATE sessions
SET title = '#' || seq_num::TEXT || ' ' ||
    CASE COALESCE(LOWER(BTRIM(source)), '')
        WHEN 'agent-sdk' THEN 'Agent SDK'
        WHEN 'agy' THEN 'AGY'
        WHEN 'claude' THEN 'Claude'
        WHEN 'claude code' THEN 'Claude'
        WHEN 'codex' THEN 'Codex'
        WHEN 'dispatcher_launcher' THEN 'Dispatcher'
        WHEN 'droid' THEN 'Droid'
        WHEN 'grok' THEN 'Grok'
        WHEN 'pipeline' THEN 'Pipeline'
        WHEN 'qwen' THEN 'Qwen'
        WHEN 'unknown' THEN 'Unknown'
        WHEN 'web_launcher' THEN 'Web'
        WHEN '' THEN 'Unknown'
        ELSE COALESCE(
            NULLIF(
                INITCAP(REGEXP_REPLACE(BTRIM(source), '[^[:alnum:]]+', ' ', 'g')),
                ''
            ),
            'Unknown'
        )
    END,
    title_source = 'provisional'
WHERE seq_num IS NOT NULL
  AND (title_source IS NULL OR title_source IN ('heuristic', 'native', 'provisional'));

ALTER TABLE sessions
DROP COLUMN IF EXISTS last_title_synthesis_digest_hash;
