"""Migration action helpers for storage schema upgrades."""

from gobby.sessions.model_family import normalize_model
from gobby.storage.database import LocalDatabase


def _setup_code_symbols_fts(db: LocalDatabase, *, include_summary: bool = False) -> None:
    """Create FTS5 triggers and populate from existing data.

    The FTS5 virtual table itself is created in BASELINE_SCHEMA (or the
    migration SQL), but triggers contain semicolons inside BEGIN...END
    which break the naive ';'-split parser. So we use executescript() here.

    Args:
        db: Database instance.
        include_summary: If True, include summary column in FTS5 index.
            Set to True for v182+ schemas that have the summary column.
    """
    if include_summary:
        cols = "name, qualified_name, signature, docstring, summary"
        vals_insert = "new.name, new.qualified_name, new.signature, new.docstring, new.summary"
        vals_delete = "old.name, old.qualified_name, old.signature, old.docstring, old.summary"
    else:
        cols = "name, qualified_name, signature, docstring"
        vals_insert = "new.name, new.qualified_name, new.signature, new.docstring"
        vals_delete = "old.name, old.qualified_name, old.signature, old.docstring"

    conn = db.connection
    # Drop existing FTS table and triggers to ensure correct column set
    conn.executescript("""
        DROP TRIGGER IF EXISTS code_symbols_ai;
        DROP TRIGGER IF EXISTS code_symbols_ad;
        DROP TRIGGER IF EXISTS code_symbols_au;
        DROP TABLE IF EXISTS code_symbols_fts;
    """)
    conn.executescript(f"""
        CREATE VIRTUAL TABLE code_symbols_fts USING fts5(
            {cols},
            content='code_symbols', content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS code_symbols_ai AFTER INSERT ON code_symbols BEGIN
            INSERT INTO code_symbols_fts(rowid, {cols})
            VALUES (new.rowid, {vals_insert});
        END;

        CREATE TRIGGER IF NOT EXISTS code_symbols_ad AFTER DELETE ON code_symbols BEGIN
            INSERT INTO code_symbols_fts(code_symbols_fts, rowid, {cols})
            VALUES ('delete', old.rowid, {vals_delete});
        END;

        CREATE TRIGGER IF NOT EXISTS code_symbols_au AFTER UPDATE ON code_symbols BEGIN
            INSERT INTO code_symbols_fts(code_symbols_fts, rowid, {cols})
            VALUES ('delete', old.rowid, {vals_delete});
            INSERT INTO code_symbols_fts(rowid, {cols})
            VALUES (new.rowid, {vals_insert});
        END;

        INSERT OR IGNORE INTO code_symbols_fts(rowid, {cols})
        SELECT rowid, {cols} FROM code_symbols;
    """)


def _setup_code_content_fts(db: LocalDatabase) -> None:
    """Create FTS5 virtual table and triggers for code content chunks.

    Follows the same pattern as _setup_code_symbols_fts.
    """
    conn = db.connection
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS code_content_fts USING fts5(
            content, file_path, language,
            content='code_content_chunks', content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS code_content_ai AFTER INSERT ON code_content_chunks BEGIN
            INSERT INTO code_content_fts(rowid, content, file_path, language)
            VALUES (new.rowid, new.content, new.file_path, new.language);
        END;

        CREATE TRIGGER IF NOT EXISTS code_content_ad AFTER DELETE ON code_content_chunks BEGIN
            INSERT INTO code_content_fts(code_content_fts, rowid, content, file_path, language)
            VALUES ('delete', old.rowid, old.content, old.file_path, old.language);
        END;

        CREATE TRIGGER IF NOT EXISTS code_content_au AFTER UPDATE ON code_content_chunks BEGIN
            INSERT INTO code_content_fts(code_content_fts, rowid, content, file_path, language)
            VALUES ('delete', old.rowid, old.content, old.file_path, old.language);
            INSERT INTO code_content_fts(rowid, content, file_path, language)
            VALUES (new.rowid, new.content, new.file_path, new.language);
        END;

        INSERT OR IGNORE INTO code_content_fts(rowid, content, file_path, language)
        SELECT rowid, content, file_path, language FROM code_content_chunks;
    """)


def _setup_tasks_fts(db: LocalDatabase) -> None:
    """Create FTS5 virtual table and triggers for task search.

    Content-synced with the tasks table — triggers keep FTS5 in sync
    automatically on INSERT/UPDATE/DELETE.
    """
    conn = db.connection
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
            title, description, labels, task_type, category,
            content='tasks', content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS tasks_fts_ai AFTER INSERT ON tasks BEGIN
            INSERT INTO tasks_fts(rowid, title, description, labels, task_type, category)
            VALUES (new.rowid, new.title, new.description, new.labels, new.task_type, new.category);
        END;

        CREATE TRIGGER IF NOT EXISTS tasks_fts_ad AFTER DELETE ON tasks BEGIN
            INSERT INTO tasks_fts(tasks_fts, rowid, title, description, labels, task_type, category)
            VALUES ('delete', old.rowid, old.title, old.description, old.labels, old.task_type, old.category);
        END;

        CREATE TRIGGER IF NOT EXISTS tasks_fts_au AFTER UPDATE ON tasks BEGIN
            INSERT INTO tasks_fts(tasks_fts, rowid, title, description, labels, task_type, category)
            VALUES ('delete', old.rowid, old.title, old.description, old.labels, old.task_type, old.category);
            INSERT INTO tasks_fts(rowid, title, description, labels, task_type, category)
            VALUES (new.rowid, new.title, new.description, new.labels, new.task_type, new.category);
        END;

        INSERT OR IGNORE INTO tasks_fts(rowid, title, description, labels, task_type, category)
        SELECT rowid, title, description, labels, task_type, category FROM tasks;
    """)


def _setup_skills_fts(db: LocalDatabase) -> None:
    """Create contentless FTS5 virtual table for skill search.

    Contentless (content='') because tags and category live in a JSON
    metadata blob — triggers can't extract them reliably. Application
    code manages inserts/deletes via SkillSearch.
    """
    conn = db.connection
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
            name, description, tags_text, category,
            content='', content_rowid='rowid'
        );
    """)
    # Populate from existing skills, extracting tags/category from JSON metadata
    conn.execute("""
        INSERT OR IGNORE INTO skills_fts(rowid, name, description, tags_text, category)
        SELECT rowid, name, description,
               COALESCE(json_extract(metadata, '$.skillport.tags'), ''),
               COALESCE(
                   json_extract(metadata, '$.skillport.category'),
                   json_extract(metadata, '$.category'),
                   ''
               )
        FROM skills WHERE deleted_at IS NULL
    """)


def _setup_memories_fts(db: LocalDatabase) -> None:
    """Create FTS5 virtual table and triggers for memory search.

    Content-synced with the memories table — triggers keep FTS5 in sync
    automatically on INSERT/UPDATE/DELETE. Tags are stripped of JSON
    formatting so FTS5 indexes clean tokens.
    """
    conn = db.connection
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content, tags, memory_type, source_type,
            content='memories', content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content, tags, memory_type, source_type)
            VALUES (
                new.rowid, new.content,
                REPLACE(REPLACE(REPLACE(COALESCE(new.tags, ''), '"', ''), '[', ''), ']', ''),
                new.memory_type, COALESCE(new.source_type, '')
            );
        END;

        CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, tags, memory_type, source_type)
            VALUES (
                'delete', old.rowid, old.content,
                REPLACE(REPLACE(REPLACE(COALESCE(old.tags, ''), '"', ''), '[', ''), ']', ''),
                old.memory_type, COALESCE(old.source_type, '')
            );
        END;

        CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE OF content, tags, memory_type, source_type ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, tags, memory_type, source_type)
            VALUES (
                'delete', old.rowid, old.content,
                REPLACE(REPLACE(REPLACE(COALESCE(old.tags, ''), '"', ''), '[', ''), ']', ''),
                old.memory_type, COALESCE(old.source_type, '')
            );
            INSERT INTO memories_fts(rowid, content, tags, memory_type, source_type)
            VALUES (
                new.rowid, new.content,
                REPLACE(REPLACE(REPLACE(COALESCE(new.tags, ''), '"', ''), '[', ''), ']', ''),
                new.memory_type, COALESCE(new.source_type, '')
            );
        END;

        INSERT OR IGNORE INTO memories_fts(rowid, content, tags, memory_type, source_type)
        SELECT rowid, content,
               REPLACE(REPLACE(REPLACE(COALESCE(tags, ''), '"', ''), '[', ''), ']', ''),
               memory_type, COALESCE(source_type, '')
        FROM memories;
    """)


def _setup_fts_tables(db: LocalDatabase) -> None:
    """Set up FTS5 tables for both tasks and skills."""
    _setup_tasks_fts(db)
    _setup_skills_fts(db)


def _drop_summary_column(db: LocalDatabase) -> None:
    """Drop summary column from code_symbols and rebuild FTS without it."""
    conn = db.connection
    conn.executescript("""
        DROP TRIGGER IF EXISTS code_symbols_ai;
        DROP TRIGGER IF EXISTS code_symbols_ad;
        DROP TRIGGER IF EXISTS code_symbols_au;
        DROP TABLE IF EXISTS code_symbols_fts;
    """)
    conn.execute("ALTER TABLE code_symbols DROP COLUMN summary")
    _setup_code_symbols_fts(db)


def _add_summary_column(db: LocalDatabase) -> None:
    """Re-add summary column to code_symbols and rebuild FTS with it."""
    db.connection.execute("ALTER TABLE code_symbols ADD COLUMN summary TEXT")
    _setup_code_symbols_fts(db, include_summary=True)


def _drop_column_if_exists(db: LocalDatabase, table: str, column: str) -> None:
    """Drop a column if it exists."""
    row = db.fetchone(
        f"SELECT COUNT(*) as cnt FROM pragma_table_info('{table}') WHERE name = ?",
        (column,),
    )
    if row and row["cnt"] > 0:
        db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def _column_exists(db: LocalDatabase, table: str, column: str) -> bool:
    """Return True when given table already has target column."""
    row = db.fetchone(
        f"SELECT COUNT(*) as cnt FROM pragma_table_info('{table}') WHERE name = ?",
        (column,),
    )
    return bool(row and row["cnt"] > 0)


def _table_exists(db: LocalDatabase, table: str) -> bool:
    """Return True when given table exists."""
    row = db.fetchone(
        "SELECT COUNT(*) as cnt FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    )
    return bool(row and row["cnt"] > 0)


def _add_column_if_missing(db: LocalDatabase, table: str, column_sql: str, column: str) -> None:
    """Add a column only when it is absent."""
    if not _column_exists(db, table, column):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column_sql}")


def _migrate_sessions_sandbox_fields(db: LocalDatabase) -> None:
    """Add sandbox metadata columns and drop unshipped cli_sandbox config surface."""
    _add_column_if_missing(db, "sessions", "sandbox_enabled BOOLEAN DEFAULT 0", "sandbox_enabled")
    _add_column_if_missing(db, "sessions", "sandbox_policy_hash TEXT", "sandbox_policy_hash")
    db.execute("DELETE FROM config_store WHERE key LIKE 'cli_sandbox.%'")


def _migrate_claimed_by_session_id(db: LocalDatabase) -> None:
    """Add canonical task ownership column and heal partial application."""
    conn = db.connection
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        with db.transaction() as tx:
            if not _column_exists(db, "tasks", "claimed_by_session_id"):
                tx.execute(
                    "ALTER TABLE tasks ADD COLUMN claimed_by_session_id TEXT REFERENCES sessions(id)"
                )

            tx.execute("""
                UPDATE tasks
                SET claimed_by_session_id = assignee
                WHERE claimed_by_session_id IS NULL
                  AND assignee IS NOT NULL
                  AND EXISTS (SELECT 1 FROM sessions WHERE sessions.id = tasks.assignee)
            """)
            tx.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_claimed_session ON tasks(claimed_by_session_id)"
            )
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_agent_run_claimed_session_id(db: LocalDatabase) -> None:
    """Add persisted agent-run claim ownership and tolerate partial application."""
    if _column_exists(db, "agent_runs", "claimed_session_id"):
        return
    db.execute("ALTER TABLE agent_runs ADD COLUMN claimed_session_id TEXT REFERENCES sessions(id)")


def _tasks_claimed_session_fk_is_set_null(db: LocalDatabase) -> bool:
    """Return True when tasks.claimed_by_session_id already uses ON DELETE SET NULL."""
    rows = db.fetchall("PRAGMA foreign_key_list(tasks)")
    for row in rows:
        if (
            row["from"] == "claimed_by_session_id"
            and row["table"] == "sessions"
            and row["on_delete"] == "SET NULL"
        ):
            return True
    return False


def _migrate_tasks_claimed_session_fk_set_null(db: LocalDatabase) -> None:
    """Rebuild tasks so deleting a session clears canonical task ownership."""
    if _tasks_claimed_session_fk_is_set_null(db):
        return

    conn = db.connection
    conn.execute("DROP TABLE IF EXISTS tasks_new")
    conn.execute("""
        CREATE TABLE tasks_new (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            parent_task_id TEXT REFERENCES tasks(id),
            created_in_session_id TEXT REFERENCES sessions(id),
            claimed_by_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
            lifecycle_stage TEXT CHECK(lifecycle_stage IN ('in_progress', 'needs_review', 'review_approved')),
            closed_in_session_id TEXT REFERENCES sessions(id),
            closed_commit_sha TEXT,
            closed_at TEXT,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'open',
            priority INTEGER DEFAULT 2,
            task_type TEXT DEFAULT 'task',
            assignee TEXT,
            labels TEXT,
            closed_reason TEXT,
            compacted_at TEXT,
            validation_status TEXT CHECK(validation_status IN ('pending', 'valid', 'invalid')),
            validation_feedback TEXT,
            validation_override_reason TEXT,
            category TEXT,
            validation_criteria TEXT,
            validation_fail_count INTEGER DEFAULT 0,
            dispatch_failure_count INTEGER DEFAULT 0,
            commits TEXT,
            escalated_at TEXT,
            escalation_reason TEXT,
            github_issue_number INTEGER,
            github_pr_number INTEGER,
            github_repo TEXT,
            linear_issue_id TEXT,
            linear_team_id TEXT,
            seq_num INTEGER,
            path_cache TEXT,
            start_date TEXT,
            due_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        INSERT INTO tasks_new (
            id, project_id, parent_task_id, created_in_session_id, claimed_by_session_id,
            lifecycle_stage, closed_in_session_id, closed_commit_sha, closed_at, title,
            description, status, priority, task_type, assignee, labels, closed_reason,
            compacted_at, validation_status, validation_feedback, validation_override_reason,
            category, validation_criteria, validation_fail_count, dispatch_failure_count,
            commits, escalated_at, escalation_reason, github_issue_number, github_pr_number,
            github_repo, linear_issue_id, linear_team_id, seq_num, path_cache,
            start_date, due_date, created_at, updated_at
        )
        SELECT
            id, project_id, parent_task_id, created_in_session_id, claimed_by_session_id,
            lifecycle_stage, closed_in_session_id, closed_commit_sha, closed_at, title,
            description, status, priority, task_type, assignee, labels, closed_reason,
            compacted_at, validation_status, validation_feedback, validation_override_reason,
            category, validation_criteria, validation_fail_count, dispatch_failure_count,
            commits, escalated_at, escalation_reason, github_issue_number, github_pr_number,
            github_repo, linear_issue_id, linear_team_id, seq_num, path_cache,
            start_date, due_date, created_at, updated_at
        FROM tasks
    """)
    conn.execute("DROP TABLE tasks")
    conn.execute("ALTER TABLE tasks_new RENAME TO tasks")
    conn.execute("CREATE INDEX idx_tasks_project ON tasks(project_id)")
    conn.execute("CREATE INDEX idx_tasks_status ON tasks(status)")
    conn.execute("CREATE INDEX idx_tasks_parent ON tasks(parent_task_id)")
    conn.execute("CREATE INDEX idx_tasks_created_session ON tasks(created_in_session_id)")
    conn.execute("CREATE INDEX idx_tasks_claimed_session ON tasks(claimed_by_session_id)")
    conn.execute("CREATE INDEX idx_tasks_lifecycle_stage ON tasks(lifecycle_stage)")
    conn.execute("CREATE INDEX idx_tasks_closed_session ON tasks(closed_in_session_id)")
    conn.execute("CREATE UNIQUE INDEX idx_tasks_seq_num ON tasks(project_id, seq_num)")
    conn.execute("CREATE INDEX idx_tasks_path_cache ON tasks(path_cache)")
    conn.execute("DROP TRIGGER IF EXISTS tasks_fts_ai")
    conn.execute("DROP TRIGGER IF EXISTS tasks_fts_ad")
    conn.execute("DROP TRIGGER IF EXISTS tasks_fts_au")
    conn.execute("DROP TABLE IF EXISTS tasks_fts")

    _setup_tasks_fts(db)


def _migrate_task_lifecycle_stage(db: LocalDatabase) -> None:
    """Add canonical lifecycle stage storage and backfill projected status."""
    with db.transaction() as tx:
        if not _column_exists(db, "tasks", "lifecycle_stage"):
            tx.execute(
                """
                ALTER TABLE tasks
                ADD COLUMN lifecycle_stage TEXT
                CHECK(lifecycle_stage IN ('in_progress', 'needs_review', 'review_approved'))
                """
            )

        tx.execute("""
            UPDATE tasks
            SET lifecycle_stage = CASE status
                WHEN 'in_progress' THEN 'in_progress'
                WHEN 'needs_review' THEN 'needs_review'
                WHEN 'review_approved' THEN 'review_approved'
                ELSE NULL
            END
            WHERE lifecycle_stage IS NULL
        """)

        tx.execute("""
            UPDATE tasks
            SET closed_at = COALESCE(closed_at, updated_at, created_at)
            WHERE status = 'closed' AND closed_at IS NULL
        """)

        tx.execute("""
            UPDATE tasks
            SET escalated_at = COALESCE(escalated_at, updated_at, created_at)
            WHERE status = 'escalated' AND escalated_at IS NULL
        """)

        tx.execute("""
            UPDATE tasks
            SET status = CASE
                WHEN closed_at IS NOT NULL THEN 'closed'
                WHEN escalated_at IS NOT NULL THEN 'escalated'
                WHEN lifecycle_stage IS NOT NULL THEN lifecycle_stage
                ELSE 'open'
            END
        """)
        tx.execute("CREATE INDEX IF NOT EXISTS idx_tasks_lifecycle_stage ON tasks(lifecycle_stage)")


def _migrate_expansion_runs(db: LocalDatabase) -> None:
    """Create expansion_runs and remove legacy task-attached expansion fields."""
    with db.transaction() as tx:
        tx.execute("""
            CREATE TABLE IF NOT EXISTS expansion_runs (
                id TEXT PRIMARY KEY,
                parent_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                triggering_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN (
                        'pending', 'running', 'compiled', 'applying',
                        'completed', 'failed', 'cancelled'
                    )),
                input_source TEXT NOT NULL
                    CHECK(input_source IN ('task', 'plan')),
                plan_file TEXT,
                provider TEXT,
                model TEXT,
                options_json TEXT,
                compiled_spec_json TEXT,
                qa_result_json TEXT,
                task_id_map_json TEXT,
                created_task_ids_json TEXT,
                error TEXT,
                logs_json TEXT,
                checkpoints_json TEXT,
                started_at TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        tx.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_expansion_runs_parent_task
            ON expansion_runs(parent_task_id, created_at DESC)
            """
        )
        tx.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_expansion_runs_status
            ON expansion_runs(status, created_at DESC)
            """
        )

    _drop_column_if_exists(db, "tasks", "expansion_context")
    _drop_column_if_exists(db, "tasks", "expansion_status")


def _drop_agent_runs_mode(db: LocalDatabase) -> None:
    """Drop the mode column from agent_runs via table rebuild."""
    db.execute("PRAGMA foreign_keys=OFF")
    db.connection.executescript("""
        DROP TABLE IF EXISTS agent_runs_new;
        CREATE TABLE agent_runs_new (
            id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL REFERENCES sessions(id),
            child_session_id TEXT REFERENCES sessions(id),
            workflow_name TEXT,
            provider TEXT DEFAULT 'claude',
            model TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            prompt TEXT,
            result TEXT,
            error TEXT,
            tool_calls_count INTEGER DEFAULT 0,
            turns_used INTEGER DEFAULT 0,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            sdk_session_id TEXT,
            continuation_prompt TEXT,
            task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
            pid INTEGER,
            tmux_session_name TEXT,
            worktree_id TEXT,
            clone_id TEXT,
            timeout_seconds REAL
        );
        INSERT INTO agent_runs_new SELECT
            id, parent_session_id, child_session_id, workflow_name,
            provider, model, status, prompt, result, error,
            tool_calls_count, turns_used, started_at, completed_at,
            created_at, updated_at, sdk_session_id, continuation_prompt,
            task_id, pid, tmux_session_name, worktree_id, clone_id,
            timeout_seconds
        FROM agent_runs;
        DROP TABLE agent_runs;
        ALTER TABLE agent_runs_new RENAME TO agent_runs;
        CREATE INDEX idx_agent_runs_parent_session ON agent_runs(parent_session_id);
        CREATE INDEX idx_agent_runs_child_session ON agent_runs(child_session_id);
        CREATE INDEX idx_agent_runs_status ON agent_runs(status);
    """)
    db.execute("PRAGMA foreign_keys=ON")


def _migrate_agent_run_reasoning_fields(db: LocalDatabase) -> None:
    """Add spawned-agent reasoning metadata columns to agent_runs."""
    additions = (
        ("requested_reasoning_effort", "TEXT"),
        ("effective_reasoning_effort", "TEXT"),
        ("reasoning_required", "INTEGER NOT NULL DEFAULT 0"),
        ("reasoning_status", "TEXT NOT NULL DEFAULT 'not_requested'"),
        ("reasoning_message", "TEXT"),
    )
    with db.transaction():
        for column, ddl in additions:
            if not _column_exists(db, "agent_runs", column):
                db.execute(f"ALTER TABLE agent_runs ADD COLUMN {column} {ddl}")


_CODE_CALLS_CREATE = """
    CREATE TABLE IF NOT EXISTS code_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL,
        caller_symbol_id TEXT NOT NULL,
        callee_symbol_id TEXT NOT NULL DEFAULT '',
        callee_name TEXT NOT NULL,
        callee_target_kind TEXT NOT NULL DEFAULT 'unresolved',
        callee_external_module TEXT NOT NULL DEFAULT '',
        file_path TEXT NOT NULL,
        line INTEGER NOT NULL DEFAULT 0,
        UNIQUE(
            project_id,
            caller_symbol_id,
            callee_symbol_id,
            callee_name,
            callee_target_kind,
            callee_external_module,
            file_path,
            line
        )
    )
"""

_CODE_CALLS_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_cc_file ON code_calls(project_id, file_path)",
    "CREATE INDEX IF NOT EXISTS idx_cc_caller ON code_calls(project_id, caller_symbol_id)",
    (
        "CREATE INDEX IF NOT EXISTS idx_cc_target "
        "ON code_calls(project_id, callee_target_kind, callee_symbol_id, callee_name)"
    ),
)


def _migrate_code_graph_target_schema(db: LocalDatabase) -> None:
    """Add code-graph attempt tracking and canonical call-target columns."""
    _add_column_if_missing(
        db,
        "code_indexed_files",
        "graph_sync_attempted_at TEXT",
        "graph_sync_attempted_at",
    )

    conn = db.connection
    if not _table_exists(db, "code_calls"):
        conn.execute(_CODE_CALLS_CREATE)
        for index_sql in _CODE_CALLS_INDEX_STATEMENTS:
            conn.execute(index_sql)
        return

    if _column_exists(db, "code_calls", "callee_target_kind"):
        for index_sql in _CODE_CALLS_INDEX_STATEMENTS:
            conn.execute(index_sql)
        return

    conn.execute("ALTER TABLE code_calls RENAME TO code_calls_legacy")
    conn.execute("""
        CREATE TABLE code_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            caller_symbol_id TEXT NOT NULL,
            callee_symbol_id TEXT NOT NULL DEFAULT '',
            callee_name TEXT NOT NULL,
            callee_target_kind TEXT NOT NULL DEFAULT 'unresolved',
            callee_external_module TEXT NOT NULL DEFAULT '',
            file_path TEXT NOT NULL,
            line INTEGER NOT NULL DEFAULT 0,
            UNIQUE(
                project_id,
                caller_symbol_id,
                callee_symbol_id,
                callee_name,
                callee_target_kind,
                callee_external_module,
                file_path,
                line
            )
        )
    """)
    conn.execute("""
        INSERT INTO code_calls (
            project_id,
            caller_symbol_id,
            callee_symbol_id,
            callee_name,
            callee_target_kind,
            callee_external_module,
            file_path,
            line
        )
        SELECT
            project_id,
            caller_symbol_id,
            '',
            callee_name,
            'unresolved',
            '',
            file_path,
            line
        FROM code_calls_legacy
    """)
    conn.execute("DROP TABLE code_calls_legacy")
    conn.execute("CREATE INDEX idx_cc_file ON code_calls(project_id, file_path)")
    conn.execute("CREATE INDEX idx_cc_caller ON code_calls(project_id, caller_symbol_id)")
    conn.execute(
        "CREATE INDEX idx_cc_target "
        "ON code_calls(project_id, callee_target_kind, callee_symbol_id, callee_name)"
    )


def _migrate_add_token_events(db: LocalDatabase) -> None:
    """Add token_events ledger and synthetic backfill rows for existing session usage."""
    conn = db.connection
    conn.execute("""
        CREATE TABLE IF NOT EXISTS token_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            project_id TEXT,
            message_id TEXT,
            source TEXT NOT NULL,
            origin TEXT NOT NULL,
            model TEXT,
            model_family TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            context_window INTEGER,
            event_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            metadata TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token_events_event_at ON token_events(event_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_token_events_session ON token_events(session_id, event_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_token_events_project_event "
        "ON token_events(project_id, event_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_token_events_model_family "
        "ON token_events(model_family, event_at)"
    )
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_token_events_dedup
            ON token_events(session_id, message_id)
            WHERE message_id IS NOT NULL
    """)

    existing_rows = db.fetchone("SELECT COUNT(*) AS count FROM token_events")
    if existing_rows and int(existing_rows["count"] or 0) > 0:
        return

    sessions = db.fetchall(
        """
        SELECT
            id,
            project_id,
            source,
            model,
            context_window,
            created_at,
            usage_input_tokens,
            usage_output_tokens,
            usage_cache_creation_tokens,
            usage_cache_read_tokens
        FROM sessions
        WHERE COALESCE(usage_input_tokens, 0) > 0
           OR COALESCE(usage_output_tokens, 0) > 0
           OR COALESCE(usage_cache_creation_tokens, 0) > 0
           OR COALESCE(usage_cache_read_tokens, 0) > 0
        """
    )
    if sessions:
        rows: list[tuple[object, ...]] = []
        for session in sessions:
            created_at = session["created_at"]
            created_at_str = None if created_at is None else str(created_at).replace("+00:00", "Z")
            rows.append(
                (
                    session["id"],
                    session["project_id"],
                    f"{session['id']}:backfill",
                    session["source"] or "backfill",
                    "backfill",
                    session["model"],
                    normalize_model(session["model"]),
                    session["usage_input_tokens"] or 0,
                    session["usage_output_tokens"] or 0,
                    session["usage_cache_creation_tokens"] or 0,
                    session["usage_cache_read_tokens"] or 0,
                    session["context_window"],
                    created_at_str,
                )
            )
        with db.transaction():
            db.executemany(
                """
                INSERT OR IGNORE INTO token_events (
                    session_id,
                    project_id,
                    message_id,
                    source,
                    origin,
                    model,
                    model_family,
                    input_tokens,
                    output_tokens,
                    cache_creation_tokens,
                    cache_read_tokens,
                    context_window,
                    event_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    session_sums = db.fetchone(
        """
        SELECT
            COALESCE(SUM(usage_input_tokens), 0) AS input_tokens,
            COALESCE(SUM(usage_output_tokens), 0) AS output_tokens,
            COALESCE(SUM(usage_cache_creation_tokens), 0) AS cache_creation_tokens,
            COALESCE(SUM(usage_cache_read_tokens), 0) AS cache_read_tokens
        FROM sessions
        """
    )
    event_sums = db.fetchone(
        """
        SELECT
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens
        FROM token_events
        """
    )
    if session_sums is None or event_sums is None:
        raise RuntimeError("token_events backfill verification failed: missing aggregate rows")
    for key in ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"):
        if int(session_sums[key] or 0) != int(event_sums[key] or 0):
            raise RuntimeError(
                f"token_events backfill mismatch for {key}: "
                f"{session_sums[key]} != {event_sums[key]}"
            )


def _add_prune_empty_session_indexes(db: LocalDatabase) -> None:
    """Add missing indexes used by prune_empty_sessions()."""
    for statement in (
        """
        CREATE INDEX IF NOT EXISTS idx_sessions_prune_status_updated_at
            ON sessions(status, updated_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_sessions_parent_session
            ON sessions(parent_session_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_memories_source_session
            ON memories(source_session_id)
        """,
    ):
        db.execute(statement)


def _narrow_memories_fts_update_trigger(db: LocalDatabase) -> None:
    """Recreate memories_fts_au trigger scoped to indexed columns only."""
    conn = db.connection
    conn.executescript("""
        DROP TRIGGER IF EXISTS memories_fts_au;

        CREATE TRIGGER memories_fts_au
        AFTER UPDATE OF content, tags, memory_type, source_type ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, tags, memory_type, source_type)
            VALUES (
                'delete', old.rowid, old.content,
                REPLACE(REPLACE(REPLACE(COALESCE(old.tags, ''), '"', ''), '[', ''), ']', ''),
                old.memory_type, COALESCE(old.source_type, '')
            );
            INSERT INTO memories_fts(rowid, content, tags, memory_type, source_type)
            VALUES (
                new.rowid, new.content,
                REPLACE(REPLACE(REPLACE(COALESCE(new.tags, ''), '"', ''), '[', ''), ']', ''),
                new.memory_type, COALESCE(new.source_type, '')
            );
        END;
    """)


def _remove_usd_columns(db: LocalDatabase) -> None:
    """Drop all USD-related columns from sessions, savings_ledger, and model_costs."""
    for table, column in [
        ("sessions", "usage_total_cost_usd"),
        ("savings_ledger", "cost_saved_usd"),
        ("model_costs", "input_cost_per_token"),
        ("model_costs", "output_cost_per_token"),
        ("model_costs", "cache_read_cost_per_token"),
        ("model_costs", "cache_creation_cost_per_token"),
    ]:
        _drop_column_if_exists(db, table, column)
