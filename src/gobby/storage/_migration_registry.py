"""Ordered migration registry for storage schema upgrades."""

from collections.abc import Callable

from gobby.storage._migration_actions import (
    _add_column_if_missing,
    _add_prune_empty_session_indexes,
    _add_summary_column,
    _drop_agent_runs_mode,
    _drop_column_if_exists,
    _drop_summary_column,
    _migrate_add_token_events,
    _migrate_agent_run_claimed_session_id,
    _migrate_agent_run_reasoning_fields,
    _migrate_claimed_by_session_id,
    _migrate_code_graph_target_schema,
    _migrate_expansion_runs,
    _migrate_sessions_sandbox_fields,
    _migrate_task_lifecycle_stage,
    _migrate_tasks_claimed_session_fk_set_null,
    _narrow_memories_fts_update_trigger,
    _remove_usd_columns,
    _setup_fts_tables,
    _setup_memories_fts,
)
from gobby.storage.database import LocalDatabase

MigrationAction = str | Callable[[LocalDatabase], None]


# Migrations beyond v171.
# Add new migrations here. Do not modify the baseline schema.
MIGRATIONS: list[tuple[int, str, MigrationAction]] = [
    (
        172,
        "Add chat_messages table for web chat display persistence",
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            tool_calls_json TEXT,
            metadata_json TEXT,
            seq INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_chat_messages_conv_seq
            ON chat_messages(conversation_id, seq);
        """,
    ),
    (
        173,
        "Add communications tables",
        """
        CREATE TABLE IF NOT EXISTS comms_channels (
            id TEXT PRIMARY KEY,
            channel_type TEXT NOT NULL,
            name TEXT NOT NULL UNIQUE,
            enabled INTEGER DEFAULT 1,
            config_json TEXT NOT NULL DEFAULT '{}',
            webhook_secret TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS comms_identities (
            id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL REFERENCES comms_channels(id) ON DELETE CASCADE,
            external_user_id TEXT NOT NULL,
            external_username TEXT,
            session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
            project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(channel_id, external_user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_comms_identities_channel ON comms_identities(channel_id);
        CREATE INDEX IF NOT EXISTS idx_comms_identities_external_user ON comms_identities(external_user_id);
        CREATE INDEX IF NOT EXISTS idx_comms_identities_session ON comms_identities(session_id);

        CREATE TABLE IF NOT EXISTS comms_messages (
            id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL REFERENCES comms_channels(id) ON DELETE CASCADE,
            identity_id TEXT REFERENCES comms_identities(id) ON DELETE SET NULL,
            direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
            content TEXT NOT NULL,
            content_type TEXT NOT NULL DEFAULT 'text',
            platform_message_id TEXT,
            platform_thread_id TEXT,
            session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
            status TEXT NOT NULL DEFAULT 'sent',
            error TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_comms_messages_channel_created ON comms_messages(channel_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_comms_messages_session ON comms_messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_comms_messages_direction ON comms_messages(direction);

        CREATE TABLE IF NOT EXISTS comms_routing_rules (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            channel_id TEXT REFERENCES comms_channels(id) ON DELETE CASCADE,
            event_pattern TEXT NOT NULL DEFAULT '*',
            project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
            session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
            priority INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            config_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_comms_routing_rules_channel ON comms_routing_rules(channel_id);
        CREATE INDEX IF NOT EXISTS idx_comms_routing_rules_enabled ON comms_routing_rules(enabled);
        """,
    ),
    (
        174,
        "Add comms_attachments table for file attachments",
        """
        CREATE TABLE IF NOT EXISTS comms_attachments (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL REFERENCES comms_messages(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            local_path TEXT,
            platform_url TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_comms_attachments_message ON comms_attachments(message_id);
        """,
    ),
    (
        175,
        "Add cleanup_after column to worktrees table",
        """
        ALTER TABLE worktrees ADD COLUMN cleanup_after TEXT;
        """,
    ),
    (
        176,
        "Drop savings_daily table (rollup replaced by direct ledger queries)",
        """
        DROP TABLE IF EXISTS savings_daily;
        """,
    ),
    (
        177,
        "Add total_eligible_files column to code_indexed_projects",
        """
        ALTER TABLE code_indexed_projects ADD COLUMN total_eligible_files INTEGER;
        """,
    ),
    (
        178,
        "Add graph_synced column to code_indexed_files",
        """
        ALTER TABLE code_indexed_files ADD COLUMN graph_synced INTEGER NOT NULL DEFAULT 0;
        CREATE INDEX idx_cif_graph_synced ON code_indexed_files(project_id, graph_synced);
        """,
    ),
    (
        179,
        "Drop summary column from code_symbols and rebuild FTS",
        _drop_summary_column,
    ),
    (
        180,
        "Rename web_chat channel type to gobby_chat",
        "UPDATE comms_channels SET channel_type = 'gobby_chat' WHERE channel_type = 'web_chat'",
    ),
    (
        181,
        "Add FTS5 search tables for tasks and skills",
        _setup_fts_tables,
    ),
    (
        182,
        "Re-add summary column to code_symbols and rebuild FTS",
        _add_summary_column,
    ),
    (
        183,
        "Add vectors_synced column and import/call relation tables",
        """
        ALTER TABLE code_indexed_files ADD COLUMN vectors_synced INTEGER NOT NULL DEFAULT 0;
        UPDATE code_indexed_files SET vectors_synced = 1;
        CREATE INDEX idx_cif_vectors_synced ON code_indexed_files(project_id, vectors_synced);

        CREATE TABLE IF NOT EXISTS code_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            source_file TEXT NOT NULL,
            target_module TEXT NOT NULL,
            UNIQUE(project_id, source_file, target_module)
        );
        CREATE INDEX IF NOT EXISTS idx_ci_file ON code_imports(project_id, source_file);

        CREATE TABLE IF NOT EXISTS code_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            caller_symbol_id TEXT NOT NULL,
            callee_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            line INTEGER NOT NULL DEFAULT 0,
            UNIQUE(project_id, caller_symbol_id, callee_name, file_path, line)
        );
        CREATE INDEX IF NOT EXISTS idx_cc_file ON code_calls(project_id, file_path);
        """,
    ),
    (
        184,
        "Eliminate skill template rows — merge into installed",
        """
        DELETE FROM skills
        WHERE source = 'template'
          AND EXISTS (
            SELECT 1 FROM skills AS s2
            WHERE s2.name = skills.name
              AND s2.source = 'installed'
              AND COALESCE(s2.project_id, '') = COALESCE(skills.project_id, '')
          );

        UPDATE skills
        SET source = 'installed', enabled = 1
        WHERE source = 'template';

        DELETE FROM skill_files
        WHERE skill_id NOT IN (SELECT id FROM skills);
        """,
    ),
    (
        185,
        "Doom loop detection: checkpoints table + dispatch_failure_count",
        """
        CREATE TABLE IF NOT EXISTS checkpoints (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            ref_name TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            parent_sha TEXT NOT NULL,
            files_changed INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT 'auto-checkpoint',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_checkpoints_task
            ON checkpoints(task_id, created_at DESC);

        ALTER TABLE tasks ADD COLUMN dispatch_failure_count INTEGER DEFAULT 0;
        """,
    ),
    (
        186,
        "Rename agent definition modes: terminal→interactive, self→inherit",
        """
        UPDATE workflow_definitions
        SET definition_json = json_set(definition_json, '$.mode', 'interactive'),
            updated_at = datetime('now')
        WHERE workflow_type = 'agent'
          AND json_extract(definition_json, '$.mode') = 'terminal';

        UPDATE workflow_definitions
        SET definition_json = json_set(definition_json, '$.mode', 'inherit'),
            updated_at = datetime('now')
        WHERE workflow_type = 'agent'
          AND json_extract(definition_json, '$.mode') = 'self';

        UPDATE agent_runs SET mode = 'interactive' WHERE mode = 'terminal';
        UPDATE agent_runs SET mode = 'inherit' WHERE mode = 'self';
        """,
    ),
    (
        187,
        "Ensure dispatch_failure_count has no NULLs",
        """
        UPDATE tasks SET dispatch_failure_count = 0 WHERE dispatch_failure_count IS NULL;
        """,
    ),
    (
        188,
        "Add NOT NULL to agent_runs.mode and indexes on checkpoints FK columns",
        """
        UPDATE agent_runs SET mode = 'interactive' WHERE mode IS NULL;

        CREATE INDEX IF NOT EXISTS idx_checkpoints_session ON checkpoints(session_id);
        CREATE INDEX IF NOT EXISTS idx_checkpoints_run ON checkpoints(run_id);
        """,
    ),
    (
        189,
        "Fix agent_runs mode CHECK: replace 'background' with 'in_process'",
        """
        UPDATE agent_runs SET mode = 'interactive' WHERE mode = 'background';
        """,
    ),
    (
        190,
        "Rebuild checkpoints table with nullable session_id",
        """
        CREATE TABLE checkpoints_new (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            ref_name TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            parent_sha TEXT NOT NULL,
            files_changed INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT 'auto-checkpoint',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO checkpoints_new SELECT * FROM checkpoints;
        DROP TABLE checkpoints;
        ALTER TABLE checkpoints_new RENAME TO checkpoints;
        CREATE INDEX idx_checkpoints_task ON checkpoints(task_id, created_at DESC);
        CREATE INDEX idx_checkpoints_session ON checkpoints(session_id);
        CREATE INDEX idx_checkpoints_run ON checkpoints(run_id);
        """,
    ),
    (
        191,
        "Migrate in_process agent mode to autonomous",
        """
        UPDATE agent_runs SET mode = 'autonomous' WHERE mode = 'in_process';
        """,
    ),
    (
        192,
        "Bootstrap system session for pipelines and cron",
        """
        INSERT OR IGNORE INTO sessions
            (id, external_id, machine_id, source, project_id, title, status, agent_depth, created_at, updated_at)
        VALUES
            ('00000000-0000-0000-0000-000000000001', 'system', 'system', 'system',
             '00000000-0000-0000-0000-000000060887', '_system', 'active', 0,
             datetime('now'), datetime('now'));
        """,
    ),
    (
        193,
        "Mark stale cron/pipeline sessions as deleted",
        """
        UPDATE sessions SET status = 'deleted', updated_at = datetime('now')
        WHERE source IN ('cron', 'pipeline') AND status != 'deleted';
        """,
    ),
    (
        194,
        "Drop mode column from agent_runs (all agents spawn via tmux)",
        _drop_agent_runs_mode,
    ),
    (
        195,
        "Add context_length/max_completion_tokens to model_costs, change source default to registry",
        """
        ALTER TABLE model_costs ADD COLUMN context_length INTEGER;
        ALTER TABLE model_costs ADD COLUMN max_completion_tokens INTEGER;
        UPDATE model_costs SET source = 'registry' WHERE source = 'litellm';
        """,
    ),
    (
        196,
        "Migrate embedding config from local/llama-cpp to Ollama OpenAI-compatible defaults",
        """
        UPDATE config_store SET value = '"nomic-embed-text"'
        WHERE key IN ('mcp_client_proxy.embedding_model', 'search.embedding_model')
        AND value = '"local/nomic-embed-text-v1.5"';

        INSERT OR IGNORE INTO config_store (key, value) VALUES
        ('mcp_client_proxy.embedding_api_base', '"http://localhost:11434/v1"');
        INSERT OR IGNORE INTO config_store (key, value) VALUES
        ('search.embedding_api_base', '"http://localhost:11434/v1"');

        UPDATE config_store SET value = '"openai-compatible"'
        WHERE key = 'mcp_client_proxy.embedding_provider'
        AND value = '"local"';
        """,
    ),
    (
        197,
        "Rename search.tfidf_weight to search.keyword_weight and map mode tfidf->keyword",
        """
        UPDATE config_store SET key = 'search.keyword_weight'
        WHERE key = 'search.tfidf_weight';

        UPDATE config_store SET value = '"keyword"'
        WHERE key = 'search.mode' AND value = '"tfidf"';
        """,
    ),
    (
        198,
        "Consolidate embedding config to embeddings.* namespace",
        """
        INSERT OR IGNORE INTO config_store (key, value)
            SELECT 'embeddings.model', value FROM config_store
            WHERE key = 'search.embedding_model';
        INSERT OR IGNORE INTO config_store (key, value)
            SELECT 'embeddings.api_base', value FROM config_store
            WHERE key = 'search.embedding_api_base';
        INSERT OR IGNORE INTO config_store (key, value)
            SELECT 'embeddings.api_key', value FROM config_store
            WHERE key = 'search.embedding_api_key';

        DELETE FROM config_store WHERE key IN (
            'search.embedding_model',
            'search.embedding_api_base',
            'search.embedding_api_key',
            'mcp_client_proxy.embedding_model',
            'mcp_client_proxy.embedding_api_base',
            'mcp_client_proxy.embedding_provider'
        );
        """,
    ),
    (
        199,
        "Drop dead table session_message_state and unused columns",
        """
        DROP TABLE IF EXISTS session_message_state;

        ALTER TABLE workflow_states DROP COLUMN task_list;
        ALTER TABLE workflow_states DROP COLUMN current_task_index;
        ALTER TABLE workflow_states DROP COLUMN files_modified_this_task;

        ALTER TABLE code_indexed_projects DROP COLUMN total_eligible_files;

        ALTER TABLE completion_subscribers DROP COLUMN subscribed_at;
        """,
    ),
    (
        200,
        "Add session_type column and update unique index",
        """
        ALTER TABLE sessions ADD COLUMN session_type TEXT NOT NULL DEFAULT 'terminal';

        UPDATE sessions SET session_type = 'web_chat' WHERE source LIKE '%web_chat%';
        UPDATE sessions SET source = 'claude' WHERE source IN ('claude_sdk', 'claude_sdk_web_chat');
        UPDATE sessions SET source = 'codex' WHERE source = 'codex_web_chat';

        DROP INDEX IF EXISTS idx_sessions_unique;
        CREATE UNIQUE INDEX idx_sessions_unique
            ON sessions(external_id, machine_id, source, project_id, session_type);
        """,
    ),
    (
        201,
        "Add FTS5 search table for memories",
        _setup_memories_fts,
    ),
    (
        202,
        "Add pending_interactions table for durable approval state",
        """
        CREATE TABLE IF NOT EXISTS pending_interactions (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            provider TEXT NOT NULL,
            tool_name TEXT,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            decision TEXT,
            response_json TEXT,
            timeout_seconds INTEGER NOT NULL DEFAULT 300,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_pending_interactions_session
            ON pending_interactions(session_id, status);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_interactions_active
            ON pending_interactions(session_id, kind)
            WHERE status = 'pending';
        """,
    ),
    (
        203,
        "Remove pending_plan_path from sessions table",
        lambda db: _drop_column_if_exists(db, "sessions", "pending_plan_path"),
    ),
    (
        204,
        "Remove USD cost tracking columns — tokens are the only unit now",
        lambda db: _remove_usd_columns(db),
    ),
    (
        205,
        "Update LM Studio embedding model to fully qualified identifier",
        """
        UPDATE config_store
        SET value = '"text-embedding-nomic-embed-text-v1.5@q8_0"'
        WHERE key = 'embeddings.model'
          AND value = '"nomic-embed-text"'
          AND EXISTS (
              SELECT 1 FROM config_store
              WHERE key = 'embeddings.api_base'
                AND value LIKE '%1234%'
          );
        """,
    ),
    (
        206,
        "Narrow memories FTS update trigger to indexed columns only",
        lambda db: _narrow_memories_fts_update_trigger(db),
    ),
    (
        207,
        "Persist agent definition name on agent runs",
        """
        ALTER TABLE agent_runs ADD COLUMN agent_name TEXT;
        """,
    ),
    (
        208,
        "Add claimed_by_session_id canonical task ownership field",
        _migrate_claimed_by_session_id,
    ),
    (
        209,
        "Add canonical task lifecycle_stage and backfill projected status",
        _migrate_task_lifecycle_stage,
    ),
    (
        210,
        "Replace task-attached expansion state with expansion_runs table",
        _migrate_expansion_runs,
    ),
    (
        211,
        "Persist agent run claimed session ownership for task recovery",
        _migrate_agent_run_claimed_session_id,
    ),
    (
        212,
        "Update tasks.claimed_by_session_id to ON DELETE SET NULL",
        _migrate_tasks_claimed_session_fk_set_null,
    ),
    (
        213,
        "Add title_source column to sessions",
        lambda db: _add_column_if_missing(db, "sessions", "title_source TEXT", "title_source"),
    ),
    (
        214,
        "Add sandbox metadata to sessions and remove cli_sandbox config",
        _migrate_sessions_sandbox_fields,
    ),
    (
        215,
        "Persist requested and effective reasoning metadata on agent_runs",
        _migrate_agent_run_reasoning_fields,
    ),
    (
        216,
        "Canonicalize code-call targets and persist graph sync attempts",
        _migrate_code_graph_target_schema,
    ),
    (
        217,
        "Add token_events ledger for event-granular token accounting",
        _migrate_add_token_events,
    ),
    (
        218,
        "Add prune_empty_sessions candidate and reference indexes",
        _add_prune_empty_session_indexes,
    ),
    (
        219,
        "Remove VoxCPM/Kokoro voice config leftovers",
        """
        UPDATE config_store
           SET value = '"chatterbox"'
         WHERE key = 'voice.tts_provider'
           AND value IN ('"voxcpm"', '"kokoro"');

        DELETE FROM config_store
         WHERE key IN (
            'voice.tts_voice',
            'voice.tts_speed',
            'voice.tts_language',
            'voice.tts_model_path',
            'voice.tts_voices_path',
            'voice.tts_voxcpm_model',
            'voice.tts_voxcpm_cfg_value',
            'voice.tts_voxcpm_inference_timesteps',
            'voice.tts_voxcpm_load_denoiser',
            'voice.tts_voxcpm_denoise',
            'voice.tts_voxcpm_local_files_only',
            'voice.tts_voxcpm_optimize'
         );
        """,
    ),
]
