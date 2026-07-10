-- Recall-signal hub promotion (#17196, epic #17099 Phase 1b).
-- Contract: docs/contracts/memory-usefulness-label.md (§3, §5, §6).
-- Guarded with IF NOT EXISTS throughout: fresh installs replay this file on
-- top of a baseline that may already contain these tables after a future
-- flatten.

-- §3.1 request-level recall-signal features, promoted from recall_signal.jsonl.
CREATE TABLE IF NOT EXISTS recall_signal_requests (
    session_id TEXT NOT NULL,
    recall_request_id TEXT NOT NULL,
    project_id TEXT,
    caller TEXT NOT NULL,
    query TEXT,
    merged_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    returned_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    rrf_applied BOOLEAN NOT NULL DEFAULT FALSE,
    graph_synthetic_similarity_discount DOUBLE PRECISION,
    ranking_score_map JSONB NOT NULL DEFAULT '{}'::jsonb,
    graph_score_map JSONB NOT NULL DEFAULT '{}'::jsonb,
    weighting JSONB NOT NULL DEFAULT '{}'::jsonb,
    schema_version INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (session_id, recall_request_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_recall_signal_requests_request_id
    ON recall_signal_requests(recall_request_id);
CREATE INDEX IF NOT EXISTS idx_recall_signal_requests_project_created
    ON recall_signal_requests(project_id, created_at);

-- §3.1 + §3.2 per-hit features. Edge-weight component columns are nullable:
-- rows predating the component feed (schema_version < 3) carry NULLs and stay
-- valid for fits over the blended scores.
CREATE TABLE IF NOT EXISTS recall_signal_hits (
    session_id TEXT NOT NULL,
    recall_request_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    project_id TEXT,
    rank INTEGER NOT NULL,
    search_via TEXT,
    similarity DOUBLE PRECISION,
    raw_semantic_score DOUBLE PRECISION,
    temporal_decay_factor DOUBLE PRECISION,
    ranking_score DOUBLE PRECISION,
    ranking_mode TEXT,
    graph_score DOUBLE PRECISION,
    edge_cosine DOUBLE PRECISION,
    edge_support_norm DOUBLE PRECISION,
    edge_weight_blend DOUBLE PRECISION,
    edge_decay_factor DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (recall_request_id, memory_id)
);

CREATE INDEX IF NOT EXISTS idx_recall_signal_hits_memory
    ON recall_signal_hits(memory_id);
CREATE INDEX IF NOT EXISTS idx_recall_signal_hits_session
    ON recall_signal_hits(session_id);

-- §5 durable injection-outcome record, one row per (recall_request_id,
-- memory_id) for every memory in returned_ids of an injection-path recall.
-- injection_position is the 0-based rendered ordinal (NOT recall rank).
CREATE TABLE IF NOT EXISTS recall_injection_outcomes (
    session_id TEXT NOT NULL,
    recall_request_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    project_id TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('injected', 'filtered')),
    drop_reason TEXT CHECK (
        drop_reason IS NULL
        OR drop_reason IN (
            'already_injected', 'review_lesson', 'empty_content',
            'payload_empty', 'budget', 'other'
        )
    ),
    drop_detail TEXT,
    injection_position INTEGER,
    injection_group TEXT,
    turn_seq INTEGER,
    caller TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (recall_request_id, memory_id),
    CHECK ((outcome = 'injected') = (drop_reason IS NULL)),
    CHECK ((outcome = 'filtered') = (injection_position IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_recall_injection_outcomes_session
    ON recall_injection_outcomes(session_id);
CREATE INDEX IF NOT EXISTS idx_recall_injection_outcomes_memory
    ON recall_injection_outcomes(memory_id);

-- §6 append-only usefulness labels; relabeling adds rows under a new
-- protocol version, never mutates.
CREATE TABLE IF NOT EXISTS recall_usefulness (
    id BIGSERIAL PRIMARY KEY,
    project_id TEXT,
    session_id TEXT NOT NULL,
    recall_request_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    label_source TEXT NOT NULL CHECK (
        label_source IN ('llm_judge', 'ablation', 'digest', 'human')
    ),
    judge_useful BOOLEAN NOT NULL,
    judge_confidence REAL,
    judge_model TEXT,
    judge_protocol_version TEXT NOT NULL,
    position_randomized BOOLEAN NOT NULL,
    length_controlled BOOLEAN NOT NULL,
    ablation_delta REAL,
    ablation_method TEXT,
    rationale TEXT,
    feature_extractor_version TEXT,
    labeled_at TIMESTAMPTZ NOT NULL,
    UNIQUE (recall_request_id, memory_id, label_source, judge_protocol_version)
);

CREATE INDEX IF NOT EXISTS idx_recall_usefulness_memory
    ON recall_usefulness(memory_id);
CREATE INDEX IF NOT EXISTS idx_recall_usefulness_session
    ON recall_usefulness(session_id);
