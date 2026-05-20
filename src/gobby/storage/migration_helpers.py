"""Helper callables for storage baseline FTS setup."""

from gobby.storage.database import LocalDatabase


def _setup_code_symbols_fts(db: LocalDatabase, *, include_summary: bool = False) -> None:
    """Create FTS5 triggers and populate from existing data.

    The FTS5 virtual table itself is created in BASELINE_SCHEMA, but triggers
    contain semicolons inside BEGIN...END which break the naive ';'-split
    parser. So we use executescript() here.

    Args:
        db: Database instance.
        include_summary: If True, include summary column in FTS5 index.
            Set to True for schemas that have the summary column.
    """
    if include_summary:
        cols = "name, qualified_name, signature, docstring, summary"
        source_cols = (
            "source.name, source.qualified_name, source.signature, source.docstring, source.summary"
        )
        vals_insert = "new.name, new.qualified_name, new.signature, new.docstring, new.summary"
        vals_delete = "old.name, old.qualified_name, old.signature, old.docstring, old.summary"
    else:
        cols = "name, qualified_name, signature, docstring"
        source_cols = "source.name, source.qualified_name, source.signature, source.docstring"
        vals_insert = "new.name, new.qualified_name, new.signature, new.docstring"
        vals_delete = "old.name, old.qualified_name, old.signature, old.docstring"

    with db.transaction() as conn:
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

            INSERT INTO code_symbols_fts(rowid, {cols})
            SELECT source.rowid, {source_cols}
            FROM code_symbols AS source
            WHERE NOT EXISTS (
                SELECT 1 FROM code_symbols_fts WHERE code_symbols_fts.rowid = source.rowid
            );
        """)


def _setup_code_content_fts(db: LocalDatabase) -> None:
    """Create FTS5 virtual table and triggers for code content chunks."""
    with db.transaction() as conn:
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

            INSERT INTO code_content_fts(rowid, content, file_path, language)
            SELECT source.rowid, source.content, source.file_path, source.language
            FROM code_content_chunks AS source
            WHERE NOT EXISTS (
                SELECT 1 FROM code_content_fts WHERE code_content_fts.rowid = source.rowid
            );
        """)


def _setup_tasks_fts(db: LocalDatabase) -> None:
    """Create FTS5 virtual table and triggers for task search.

    Content-synced with the tasks table; triggers keep FTS5 in sync
    automatically on INSERT/UPDATE/DELETE.
    """
    with db.transaction() as conn:
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

            INSERT INTO tasks_fts(rowid, title, description, labels, task_type, category)
            SELECT source.rowid, source.title, source.description, source.labels,
                   source.task_type, source.category
            FROM tasks AS source
            WHERE NOT EXISTS (
                SELECT 1 FROM tasks_fts WHERE tasks_fts.rowid = source.rowid
            );
        """)


def _setup_skills_fts(db: LocalDatabase) -> None:
    """Create contentless FTS5 virtual table for skill search.

    Contentless (content='') because tags and category live in a JSON metadata
    blob. Application code manages inserts/deletes via SkillSearch.
    """
    with db.transaction() as conn:
        conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
                name, description, tags_text, category,
                content='', content_rowid='rowid'
            );
        """)
        conn.execute("""
            INSERT INTO skills_fts(rowid, name, description, tags_text, category)
            SELECT source.rowid, source.name, source.description,
                   COALESCE(json_extract(source.metadata, '$.skillport.tags'), ''),
                   COALESCE(
                       json_extract(source.metadata, '$.skillport.category'),
                       json_extract(source.metadata, '$.category'),
                       ''
                   )
            FROM skills AS source
            WHERE source.deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM skills_fts WHERE skills_fts.rowid = source.rowid
              )
        """)


def _setup_memories_fts(db: LocalDatabase) -> None:
    """Create FTS5 virtual table and triggers for memory search.

    Content-synced with the memories table; triggers keep FTS5 in sync
    automatically on INSERT/UPDATE/DELETE. Tags are stripped of JSON
    formatting so FTS5 indexes clean tokens.
    """
    with db.transaction() as conn:
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

            CREATE TRIGGER IF NOT EXISTS memories_fts_au
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

            INSERT INTO memories_fts(rowid, content, tags, memory_type, source_type)
            SELECT source.rowid, source.content,
                   REPLACE(REPLACE(REPLACE(COALESCE(source.tags, ''), '"', ''), '[', ''), ']', ''),
                   source.memory_type, COALESCE(source.source_type, '')
            FROM memories AS source
            WHERE NOT EXISTS (
                SELECT 1 FROM memories_fts WHERE memories_fts.rowid = source.rowid
            );
        """)
