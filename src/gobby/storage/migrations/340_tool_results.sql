CREATE TABLE tool_results (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id UUID,
    server_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    content TEXT NOT NULL,
    content_kind TEXT NOT NULL CHECK (content_kind IN ('json', 'text')),
    total_chars BIGINT NOT NULL,
    stored_chars INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_tool_results_created ON tool_results(created_at);

CREATE TABLE tool_result_chunks (
    id UUID PRIMARY KEY,
    result_id UUID NOT NULL REFERENCES tool_results(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    content TEXT NOT NULL,
    UNIQUE (result_id, ordinal)
);

CREATE INDEX tool_result_chunks_search_bm25 ON tool_result_chunks
USING bm25 (id, content) WITH (key_field='id');
