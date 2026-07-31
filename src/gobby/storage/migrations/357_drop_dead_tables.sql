-- gobby:destructive

-- Evidence block: savings_ledger
-- Read-only hub check (2026-07-31): 94,544 rows;
-- savings_ledger_id_seq.last_value=95,286; no src/crates code references.
DROP TABLE IF EXISTS savings_ledger;

-- Evidence block: session_memories
-- Read-only hub check (2026-07-31): 0 rows; session_memories_id_seq.last_value=NULL;
-- no relational reader/writer; dropping the table removes its 2 outbound FKs.
DROP TABLE IF EXISTS session_memories;

-- Evidence block: rule_overrides
-- Read-only hub check (2026-07-31): 0 rows; no owned sequence or FKs;
-- its sole executable source reference was the RuleEngine probe removed with this migration.
DROP TABLE IF EXISTS rule_overrides;

-- Evidence block: workflow_states
-- Read-only hub check (2026-07-31): 138 rows; no owned sequence;
-- no executable reader/writer; dropping the table removes its outbound session FK.
DROP TABLE IF EXISTS workflow_states;

-- Evidence block: tool_embeddings
-- Read-only hub check (2026-07-31): 0 rows; tool_embeddings_id_seq.last_value=NULL;
-- live name references target Qdrant; dropping the table removes its 2 outbound FKs.
DROP TABLE IF EXISTS tool_embeddings;
