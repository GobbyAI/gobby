-- Normalize legacy free-text memory categories before closing the storage contract.
CREATE TEMP TABLE memory_type_mapping (
    legacy_type TEXT PRIMARY KEY,
    canonical_type TEXT NOT NULL
        CHECK (canonical_type IN ('fact', 'preference', 'pattern', 'context'))
) ON COMMIT DROP;

INSERT INTO memory_type_mapping (legacy_type, canonical_type) VALUES
    ('fact', 'fact'),
    ('codebase_fact', 'fact'),
    ('project_fact', 'fact'),
    ('observation', 'fact'),
    ('insight', 'fact'),
    ('note', 'fact'),
    ('implementation_note', 'fact'),
    ('decision', 'fact'),
    ('design_decision', 'fact'),
    ('codebase_decision', 'fact'),
    ('architecture_decision', 'fact'),
    ('troubleshooting_note', 'fact'),
    ('reference', 'fact'),
    ('knowledge', 'fact'),
    ('project_knowledge', 'fact'),
    ('command', 'fact'),
    ('solution', 'fact'),
    ('workaround', 'fact'),
    ('gotcha', 'fact'),
    ('preference', 'preference'),
    ('preferences', 'preference'),
    ('user_preference', 'preference'),
    ('style_preference', 'preference'),
    ('setting', 'preference'),
    ('pattern', 'pattern'),
    ('debugging_pattern', 'pattern'),
    ('workflow_pattern', 'pattern'),
    ('code_pattern', 'pattern'),
    ('codebase_pattern', 'pattern'),
    ('test_pattern', 'pattern'),
    ('testing_pattern', 'pattern'),
    ('convention', 'pattern'),
    ('best_practice', 'pattern'),
    ('practice', 'pattern'),
    ('technique', 'pattern'),
    ('lesson', 'pattern'),
    ('learning', 'pattern'),
    ('rule', 'pattern'),
    ('context', 'context'),
    ('project_context', 'context'),
    ('session_context', 'context'),
    ('background', 'context'),
    ('environment', 'context'),
    ('state', 'context'),
    ('constraint', 'context'),
    ('configuration', 'context'),
    ('config', 'context');

WITH normalized AS (
    SELECT
        id,
        trim(
            both '_' from lower(regexp_replace(btrim(memory_type), '[^[:alnum:]]+', '_', 'g'))
        ) AS legacy_type
    FROM memories
), mapped AS (
    SELECT
        normalized.id,
        COALESCE(memory_type_mapping.canonical_type, 'fact') AS canonical_type
    FROM normalized
    LEFT JOIN memory_type_mapping
        ON memory_type_mapping.legacy_type = normalized.legacy_type
)
UPDATE memories
SET memory_type = mapped.canonical_type
FROM mapped
WHERE memories.id = mapped.id
  AND memories.memory_type IS DISTINCT FROM mapped.canonical_type;

-- Existing Qdrant points predate the canonical memory_type payload.
UPDATE memories SET vector_needs_reindex = TRUE;

ALTER TABLE memories
    DROP CONSTRAINT IF EXISTS memories_memory_type_check;
ALTER TABLE memories
    ADD CONSTRAINT memories_memory_type_check
    CHECK (memory_type IN ('fact', 'preference', 'pattern', 'context'));
