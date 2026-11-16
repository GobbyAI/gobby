-- Retire the old terminal automation namespace and observer settings.
WITH removed AS (
    DELETE FROM config_store
    WHERE key = 'tmux'
       OR key LIKE 'tmux.%'
       OR key IN (
           'terminal_host.tmux_attach_history_max_bytes',
           'terminal_host.tmux_poll_interval_ms',
           'terminal_host.tmux_poll_backoff_ceiling_ms',
           'terminal_host.max_attached_terminals'
       )
    RETURNING key
)
UPDATE config_state SET revision = revision + 1
WHERE id = true AND EXISTS (SELECT 1 FROM removed);

-- Agent runs retain their history after their retired terminal is removed.
UPDATE agent_runs SET terminal_id = NULL
WHERE terminal_id IN (SELECT id FROM terminals WHERE backend <> 'native');

DELETE FROM terminals WHERE backend <> 'native';
