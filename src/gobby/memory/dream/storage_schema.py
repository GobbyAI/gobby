"""Runtime schema lifecycle for memory dream storage."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase


def ensure_dream_schema(db: HubDatabase) -> None:
    """Create dream tables for upgraded daemons that have not migrated yet."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_dream_runs (
            id UUID PRIMARY KEY,
            project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'started'
                CONSTRAINT memory_dream_runs_status_check
                CHECK (
                    status IN (
                        'started', 'running', 'completed', 'failed', 'reverted',
                        'revert_failed', 'interrupted', 'partial'
                    )
                ),
            dry_run BOOLEAN NOT NULL DEFAULT FALSE,
            options JSONB NOT NULL DEFAULT '{}'::jsonb,
            plan JSONB,
            summary JSONB,
            checkpoint JSONB,
            error TEXT,
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            reverted_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    db.execute("ALTER TABLE memory_dream_runs ADD COLUMN IF NOT EXISTS checkpoint JSONB")
    # Constraint repair for pre-'partial' tables lives in migration 348;
    # runtime schema setup only creates missing objects.
    db.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('idx_memory_dream_runs_single_running') IS NULL THEN
                -- Recovery ahead of index reconciliation: rows still
                -- non-terminal before the single-running index exists are
                -- orphans of a pre-admission daemon; sweep them so the
                -- unique index can build.
                UPDATE memory_dream_runs
                   SET status = 'interrupted',
                       error = 'Interrupted: daemon restarted while the dream run was in progress',
                       completed_at = COALESCE(completed_at, NOW()),
                       updated_at = NOW()
                 WHERE status IN ('started', 'running');
                CREATE UNIQUE INDEX idx_memory_dream_runs_single_running
                    ON memory_dream_runs (status)
                    WHERE status = 'running';
            END IF;
        END $$;
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_dream_snapshots (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            run_id UUID NOT NULL REFERENCES memory_dream_runs(id)
                ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
            memory_id UUID NOT NULL,
            action TEXT NOT NULL
                CONSTRAINT memory_dream_snapshots_action_check
                CHECK (
                    action IN (
                        'keep', 'delete', 'refresh', 'merge', 'supersede', 'review',
                        'promote'
                    )
                ),
            before_data JSONB,
            after_data JSONB,
            applied BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_dream_snapshots_run
        ON memory_dream_snapshots(run_id)
        """
    )
    db.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                  FROM pg_class cls
                  JOIN pg_attribute attr
                    ON attr.attrelid = cls.oid
                   AND attr.attname = 'status'
                  JOIN pg_attrdef def
                    ON def.adrelid = attr.attrelid
                   AND def.adnum = attr.attnum
                 WHERE cls.oid = 'memory_dream_runs'::regclass
                   AND pg_get_expr(def.adbin, def.adrelid) = '''started''::text'
            ) THEN
                ALTER TABLE memory_dream_runs ALTER COLUMN status SET DEFAULT 'started';
            END IF;
        END $$;
        """
    )
    db.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                  FROM pg_constraint
                 WHERE conname = 'memory_dream_snapshots_action_check'
                   AND conrelid = 'memory_dream_snapshots'::regclass
            ) THEN
                ALTER TABLE memory_dream_snapshots
                    ADD CONSTRAINT memory_dream_snapshots_action_check
                    CHECK (
                        action IN (
                            'keep', 'delete', 'refresh', 'merge', 'supersede', 'review',
                            'promote'
                        )
                    );
            ELSIF EXISTS (
                SELECT 1
                  FROM pg_constraint
                 WHERE conname = 'memory_dream_snapshots_action_check'
                   AND conrelid = 'memory_dream_snapshots'::regclass
                   AND pg_get_constraintdef(oid) NOT LIKE '%promote%'
            ) THEN
                ALTER TABLE memory_dream_snapshots
                    DROP CONSTRAINT memory_dream_snapshots_action_check;
                ALTER TABLE memory_dream_snapshots
                    ADD CONSTRAINT memory_dream_snapshots_action_check
                    CHECK (
                        action IN (
                            'keep', 'delete', 'refresh', 'merge', 'supersede', 'review',
                            'promote'
                        )
                    );
            END IF;
        END $$;
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_dream_truth_state (
            project_id TEXT PRIMARY KEY,
            digest_hash TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
