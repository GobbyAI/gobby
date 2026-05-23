CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    repo_path TEXT,
    github_url TEXT,
    github_repo TEXT,
    linear_team_id TEXT,
    linear_project_id TEXT,
    linear_synced_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_projects_name ON projects(name);

CREATE TABLE mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    transport TEXT NOT NULL,
    url TEXT,
    command TEXT,
    args JSONB,
    env JSONB,
    headers JSONB,
    enabled BOOLEAN DEFAULT TRUE,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_mcp_servers_name ON mcp_servers(name);

CREATE INDEX idx_mcp_servers_project_id ON mcp_servers(project_id);

CREATE INDEX idx_mcp_servers_enabled ON mcp_servers(enabled);

CREATE UNIQUE INDEX idx_mcp_servers_name_project ON mcp_servers(name, project_id);

CREATE TABLE tools (
    id TEXT PRIMARY KEY,
    mcp_server_id TEXT NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    name TEXT NOT NULL,
    description TEXT,
    input_schema JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(mcp_server_id, name)
);

CREATE INDEX idx_tools_server_id ON tools(mcp_server_id);

CREATE INDEX idx_tools_name ON tools(name);

CREATE TABLE tool_embeddings (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tool_id TEXT NOT NULL REFERENCES tools(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    server_name TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    embedding BYTEA NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL,
    text_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tool_id)
);

CREATE INDEX idx_tool_embeddings_tool ON tool_embeddings(tool_id);

CREATE INDEX idx_tool_embeddings_server ON tool_embeddings(server_name);

CREATE INDEX idx_tool_embeddings_project ON tool_embeddings(project_id);

CREATE INDEX idx_tool_embeddings_hash ON tool_embeddings(text_hash);

CREATE TABLE tool_schema_hashes (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    server_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    project_id TEXT NOT NULL,
    schema_hash TEXT NOT NULL,
    last_verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(project_id, server_name, tool_name)
);

CREATE INDEX idx_schema_hashes_server ON tool_schema_hashes(server_name);

CREATE INDEX idx_schema_hashes_project ON tool_schema_hashes(project_id);

CREATE INDEX idx_schema_hashes_verified ON tool_schema_hashes(last_verified_at);

CREATE TABLE tool_metrics (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    server_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    call_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    total_latency_ms REAL NOT NULL DEFAULT 0,
    avg_latency_ms REAL,
    last_called_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(project_id, server_name, tool_name)
);

CREATE INDEX idx_tool_metrics_project ON tool_metrics(project_id);

CREATE INDEX idx_tool_metrics_server ON tool_metrics(server_name);

CREATE INDEX idx_tool_metrics_tool ON tool_metrics(tool_name);

CREATE INDEX idx_tool_metrics_call_count ON tool_metrics(call_count DESC);

CREATE INDEX idx_tool_metrics_last_called ON tool_metrics(last_called_at);

CREATE TABLE tool_metrics_daily (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    server_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    date DATE NOT NULL,
    call_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    total_latency_ms REAL NOT NULL DEFAULT 0,
    avg_latency_ms REAL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(project_id, server_name, tool_name, date)
);

CREATE INDEX idx_tool_metrics_daily_project ON tool_metrics_daily(project_id);

CREATE INDEX idx_tool_metrics_daily_date ON tool_metrics_daily(date);

CREATE INDEX idx_tool_metrics_daily_server ON tool_metrics_daily(server_name);

CREATE TABLE agent_runs (
    id TEXT PRIMARY KEY
);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    external_id TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    source TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) DEFERRABLE INITIALLY IMMEDIATE,
    title TEXT,
    title_source TEXT,
    status TEXT DEFAULT 'active',
    transcript_path TEXT,
    summary_path TEXT,
    summary_markdown TEXT,
    git_branch TEXT,
    parent_session_id TEXT REFERENCES sessions(id) DEFERRABLE INITIALLY IMMEDIATE,
    transcript_processed BOOLEAN DEFAULT FALSE,
    agent_depth INTEGER DEFAULT 0,
    spawned_by_agent_id TEXT,
    workflow_name TEXT,
    agent_run_id TEXT REFERENCES agent_runs(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    context_injected BOOLEAN DEFAULT FALSE,
    original_prompt TEXT,
    usage_input_tokens INTEGER DEFAULT 0,
    usage_output_tokens INTEGER DEFAULT 0,
    usage_cache_creation_tokens INTEGER DEFAULT 0,
    usage_cache_read_tokens INTEGER DEFAULT 0,
    context_window INTEGER,
    terminal_context JSONB,
    seq_num INTEGER,
    model TEXT,
    is_local BOOLEAN NOT NULL DEFAULT FALSE,
    had_edits BOOLEAN DEFAULT FALSE,
    digest_markdown TEXT,
    last_turn_markdown TEXT,
    chat_mode TEXT DEFAULT 'plan',
    last_digest_input_hash TEXT,
    message_count INTEGER DEFAULT 0,
    turn_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    last_assistant_content TEXT,
    approved_tools_json JSONB,
    session_type TEXT NOT NULL DEFAULT 'terminal',
    sandbox_enabled BOOLEAN DEFAULT FALSE,
    sandbox_policy_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sessions_external_id ON sessions(external_id);

CREATE INDEX idx_sessions_machine_id ON sessions(machine_id);

CREATE INDEX idx_sessions_source ON sessions(source);

CREATE INDEX idx_sessions_status ON sessions(status);

CREATE INDEX idx_sessions_project_id ON sessions(project_id);

CREATE INDEX idx_sessions_pending_transcript ON sessions(status, transcript_processed)
    WHERE status = 'expired' AND transcript_processed = FALSE;

CREATE INDEX idx_sessions_prune_status_updated_at ON sessions(status, updated_at);

CREATE INDEX idx_sessions_parent_session ON sessions(parent_session_id);

CREATE INDEX idx_sessions_agent_depth ON sessions(agent_depth);

CREATE INDEX idx_sessions_spawned_by ON sessions(spawned_by_agent_id);

CREATE INDEX idx_sessions_workflow ON sessions(workflow_name);

CREATE INDEX idx_sessions_agent_run ON sessions(agent_run_id);

CREATE UNIQUE INDEX idx_sessions_seq_num ON sessions(project_id, seq_num);

CREATE UNIQUE INDEX idx_sessions_unique ON sessions(external_id, machine_id, source, project_id, session_type);

CREATE TABLE session_stop_signals (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    source TEXT NOT NULL,
    reason TEXT,
    requested_at TIMESTAMPTZ NOT NULL,
    acknowledged_at TIMESTAMPTZ
);

CREATE INDEX idx_stop_signals_pending ON session_stop_signals(acknowledged_at)
    WHERE acknowledged_at IS NULL;

CREATE TABLE loop_progress (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    progress_type TEXT NOT NULL,
    tool_name TEXT,
    details TEXT,
    recorded_at TIMESTAMPTZ NOT NULL,
    is_high_value BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_loop_progress_session ON loop_progress(session_id, recorded_at DESC);

CREATE INDEX idx_loop_progress_high_value ON loop_progress(session_id, is_high_value, recorded_at DESC)
    WHERE is_high_value IS TRUE;

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) DEFERRABLE INITIALLY IMMEDIATE,
    parent_task_id TEXT REFERENCES tasks(id) DEFERRABLE INITIALLY IMMEDIATE,
    created_in_session_id TEXT REFERENCES sessions(id) DEFERRABLE INITIALLY IMMEDIATE,
    claimed_by_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    closed_in_session_id TEXT REFERENCES sessions(id) DEFERRABLE INITIALLY IMMEDIATE,
    closed_commit_sha TEXT,
    closed_at TIMESTAMPTZ,
    title TEXT NOT NULL,
    description TEXT,
    priority INTEGER DEFAULT 2,
    task_type TEXT DEFAULT 'task',
    assignee TEXT,
    labels JSONB,
    closed_reason TEXT,
    compacted_at TIMESTAMPTZ,
    validation_status TEXT CHECK(validation_status IN ('pending', 'valid', 'invalid')),
    validation_feedback TEXT,
    validation_override_reason TEXT,
    category TEXT,
    validation_criteria TEXT,
    validation_fail_count INTEGER DEFAULT 0,
    dispatch_failure_count INTEGER DEFAULT 0,
    allow_automation BOOLEAN NOT NULL DEFAULT FALSE CHECK(allow_automation IN (FALSE, TRUE)),
    unattended BOOLEAN NOT NULL DEFAULT FALSE CHECK(unattended IN (FALSE, TRUE)),
    isolation TEXT NOT NULL DEFAULT 'worktree' CHECK(isolation IN ('none', 'worktree', 'clone')),
    assigned_agent TEXT,
    implementation_domain TEXT CHECK(
        implementation_domain IS NULL
        OR implementation_domain IN ('backend', 'frontend', 'fullstack')
    ),
    additional_skills JSONB,
    commits JSONB,
    escalated_at TIMESTAMPTZ,
    escalation_reason TEXT,
    github_issue_number INTEGER,
    github_pr_number INTEGER,
    github_repo TEXT,
    linear_issue_id TEXT,
    linear_team_id TEXT,
    seq_num INTEGER,
    path_cache TEXT,
    start_date DATE,
    due_date DATE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    is_escalated BOOLEAN NOT NULL DEFAULT FALSE
                    CHECK(is_escalated IN (FALSE, TRUE)),
    state_bucket TEXT NOT NULL DEFAULT 'ready'
        CHECK(state_bucket IN (
            'ready',
            'in_progress',
            'needs_review',
            'review_approved',
            'closed',
            'escalated'
        )));

CREATE INDEX idx_tasks_project ON tasks(project_id);

CREATE INDEX idx_tasks_parent ON tasks(parent_task_id);

CREATE INDEX idx_tasks_created_session ON tasks(created_in_session_id);

CREATE INDEX idx_tasks_claimed_session ON tasks(claimed_by_session_id);

CREATE INDEX idx_tasks_closed_session ON tasks(closed_in_session_id);

CREATE UNIQUE INDEX idx_tasks_seq_num ON tasks(project_id, seq_num);

CREATE UNIQUE INDEX idx_tasks_github_issue_link
    ON tasks(project_id, github_repo, github_issue_number)
    WHERE github_repo IS NOT NULL
      AND github_issue_number IS NOT NULL;

CREATE INDEX idx_tasks_path_cache ON tasks(path_cache);

ALTER TABLE agent_runs
    ADD COLUMN parent_session_id TEXT NOT NULL REFERENCES sessions(id) DEFERRABLE INITIALLY IMMEDIATE,
    ADD COLUMN child_session_id TEXT REFERENCES sessions(id) DEFERRABLE INITIALLY IMMEDIATE,
    ADD COLUMN claimed_session_id TEXT REFERENCES sessions(id) DEFERRABLE INITIALLY IMMEDIATE,
    ADD COLUMN workflow_name TEXT,
    ADD COLUMN agent_name TEXT,
    ADD COLUMN provider TEXT NOT NULL,
    ADD COLUMN model TEXT,
    ADD COLUMN is_local BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN requested_reasoning_effort TEXT,
    ADD COLUMN effective_reasoning_effort TEXT,
    ADD COLUMN reasoning_required BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN reasoning_status TEXT NOT NULL DEFAULT 'not_requested',
    ADD COLUMN reasoning_message TEXT,
    ADD COLUMN status TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN prompt TEXT NOT NULL,
    ADD COLUMN result TEXT,
    ADD COLUMN error TEXT,
    ADD COLUMN tool_calls_count INTEGER DEFAULT 0,
    ADD COLUMN turns_used INTEGER DEFAULT 0,
    ADD COLUMN started_at TIMESTAMPTZ,
    ADD COLUMN completed_at TIMESTAMPTZ,
    ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN sdk_session_id TEXT,
    ADD COLUMN continuation_prompt TEXT,
    ADD COLUMN task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    ADD COLUMN pid INTEGER,
    ADD COLUMN tmux_session_name TEXT,
    ADD COLUMN worktree_id TEXT,
    ADD COLUMN clone_id TEXT,
    ADD COLUMN timeout_seconds REAL,
    ADD COLUMN terminal_reason TEXT;

CREATE INDEX idx_agent_runs_parent_session ON agent_runs(parent_session_id);

CREATE INDEX idx_agent_runs_child_session ON agent_runs(child_session_id);

CREATE INDEX idx_agent_runs_status ON agent_runs(status);

CREATE INDEX idx_agent_runs_provider ON agent_runs(provider);

CREATE INDEX idx_agent_runs_task_id ON agent_runs(task_id);


CREATE TABLE plans (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) DEFERRABLE INITIALLY IMMEDIATE,
    plan_id TEXT NOT NULL,
    plan_path TEXT NOT NULL,
    plan_hash TEXT,
    plan_kind TEXT NOT NULL CHECK(plan_kind IN ('implementation', 'strategy')),
    state TEXT NOT NULL CHECK(state IN ('active', 'archived')),
    root_task_ref TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    archived_at TIMESTAMPTZ,
    UNIQUE (project_id, plan_id)
);

CREATE INDEX idx_plans_root_task ON plans(root_task_ref);

CREATE INDEX idx_plans_state ON plans(state);

CREATE INDEX idx_plans_project_state ON plans(project_id, state);

CREATE TABLE task_dependencies (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    depends_on TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    dep_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(task_id, depends_on, dep_type)
);

CREATE INDEX idx_deps_task ON task_dependencies(task_id);

CREATE INDEX idx_deps_depends_on ON task_dependencies(depends_on);

CREATE TABLE task_dispatch_mutex (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    lease_until TIMESTAMPTZ,
    lease_holder TEXT,
    run_id TEXT,
    action_kind TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_dispatch_mutex_scan ON task_dispatch_mutex(lease_until, run_id);
CREATE INDEX idx_dispatch_mutex_run_id ON task_dispatch_mutex(run_id);

CREATE TABLE task_lifecycle_events (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    by_actor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_lifecycle_events_task ON task_lifecycle_events(task_id, created_at);

CREATE TABLE project_lifecycle_events (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    event TEXT NOT NULL,
    reason TEXT NOT NULL,
    by_actor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_project_lifecycle_events_project
    ON project_lifecycle_events(project_id, created_at);

CREATE TABLE build_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    root_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    input_ref TEXT,
    action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'started'
        CHECK (status IN ('started', 'completed', 'failed', 'skipped')),
    actor TEXT NOT NULL DEFAULT 'build',
    summary_json JSONB,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_build_runs_project_started
    ON build_runs(project_id, started_at DESC);

CREATE INDEX idx_build_runs_root_started
    ON build_runs(root_task_id, started_at DESC);

CREATE INDEX idx_build_runs_input_started
    ON build_runs(project_id, input_ref, started_at DESC);

CREATE TABLE build_history_events (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id TEXT REFERENCES build_runs(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    root_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    event_type TEXT NOT NULL,
    action TEXT,
    message TEXT,
    payload_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_build_history_events_project
    ON build_history_events(project_id, created_at DESC);

CREATE INDEX idx_build_history_events_root
    ON build_history_events(root_task_id, created_at DESC);

CREATE INDEX idx_build_history_events_run
    ON build_history_events(run_id, created_at DESC);

CREATE TABLE expansion_runs (
    id TEXT PRIMARY KEY,
    parent_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    triggering_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'running', 'compiled', 'applying', 'completed', 'failed', 'cancelled')),
    input_source TEXT NOT NULL
        CHECK(input_source IN ('task', 'plan')),
    plan_file TEXT,
    provider TEXT,
    model TEXT,
    options_json JSONB,
    compiled_spec_json JSONB,
    qa_result_json JSONB,
    task_id_map_json JSONB,
    created_task_ids_json JSONB,
    error TEXT,
    logs_json JSONB,
    checkpoints_json JSONB,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_expansion_runs_parent_task ON expansion_runs(parent_task_id, created_at DESC);

CREATE INDEX idx_expansion_runs_status ON expansion_runs(status, created_at DESC);

CREATE TABLE session_tasks (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    action TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(session_id, task_id, action)
);

CREATE INDEX idx_session_tasks_session ON session_tasks(session_id);

CREATE INDEX idx_session_tasks_task ON session_tasks(task_id);

CREATE TABLE task_validation_history (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    iteration INTEGER NOT NULL,
    status TEXT NOT NULL,
    feedback TEXT,
    issues TEXT,
    context_type TEXT,
    context_summary TEXT,
    validator_type TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_validation_history_task ON task_validation_history(task_id);

CREATE TABLE task_selection_history (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    task_id TEXT NOT NULL,
    selected_at TIMESTAMPTZ NOT NULL,
    context JSONB
);

CREATE INDEX idx_task_selection_session ON task_selection_history(session_id, selected_at DESC);

CREATE INDEX idx_task_selection_task ON task_selection_history(session_id, task_id, selected_at DESC);

CREATE TABLE workflow_states (
    session_id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    step TEXT NOT NULL,
    step_entered_at TIMESTAMPTZ,
    step_action_count INTEGER DEFAULT 0,
    total_action_count INTEGER DEFAULT 0,
    context_injected BOOLEAN DEFAULT FALSE,
    variables JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE
);

CREATE TABLE workflow_audit_log (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    step TEXT NOT NULL,
    event_type TEXT NOT NULL,
    tool_name TEXT,
    rule_id TEXT,
    condition TEXT,
    result TEXT NOT NULL,
    reason TEXT,
    context JSONB,
    FOREIGN KEY (session_id) REFERENCES sessions(id) DEFERRABLE INITIALLY IMMEDIATE
);

CREATE INDEX idx_audit_session ON workflow_audit_log(session_id);

CREATE INDEX idx_audit_timestamp ON workflow_audit_log(timestamp);

CREATE INDEX idx_audit_event_type ON workflow_audit_log(event_type);

CREATE INDEX idx_audit_result ON workflow_audit_log(result);

CREATE TABLE workflow_instances (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    workflow_name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    priority INTEGER NOT NULL DEFAULT 100,
    current_step TEXT,
    step_entered_at TIMESTAMPTZ,
    step_action_count INTEGER DEFAULT 0,
    total_action_count INTEGER DEFAULT 0,
    variables JSONB DEFAULT '{}'::jsonb,
    context_injected BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(session_id, workflow_name),
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE
);

CREATE INDEX idx_workflow_instances_session ON workflow_instances(session_id);

CREATE INDEX idx_workflow_instances_enabled ON workflow_instances(session_id, enabled);

CREATE TABLE session_variables (
    session_id TEXT PRIMARY KEY,
    variables JSONB DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) DEFERRABLE INITIALLY IMMEDIATE,
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    source_type TEXT,
    source_session_id TEXT REFERENCES sessions(id) DEFERRABLE INITIALLY IMMEDIATE,
    access_count INTEGER DEFAULT 0,
    last_accessed_at TIMESTAMPTZ,
    tags JSONB,
    media JSONB,
    graph_processed BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_memories_project ON memories(project_id);

CREATE INDEX idx_memories_type ON memories(memory_type);

CREATE INDEX idx_memories_graph_pending ON memories(graph_processed) WHERE graph_processed IS FALSE;

CREATE INDEX idx_memories_source_session ON memories(source_session_id);

ALTER TABLE memories
ADD CONSTRAINT tags_is_array
CHECK (tags IS NULL OR jsonb_typeof(tags) = 'array');

CREATE OR REPLACE FUNCTION memories_tags_to_text(tags jsonb)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
RETURNS NULL ON NULL INPUT
AS $$
    -- ORDER BY ord is load-bearing: string_agg input order is undefined without it.
    SELECT COALESCE(string_agg(value, ' ' ORDER BY ord), '')
    FROM jsonb_array_elements_text(tags) WITH ORDINALITY AS t(value, ord);
$$;

ALTER TABLE memories
ADD COLUMN tags_text TEXT
GENERATED ALWAYS AS (memories_tags_to_text(tags)) STORED;

CREATE TABLE session_memories (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    action TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(session_id, memory_id, action)
);

CREATE INDEX idx_session_memories_session ON session_memories(session_id);

CREATE INDEX idx_session_memories_memory ON session_memories(memory_id);

CREATE TABLE memory_crossrefs (
    source_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    target_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    similarity REAL NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_id, target_id)
);

CREATE INDEX idx_crossrefs_source ON memory_crossrefs(source_id);

CREATE INDEX idx_crossrefs_target ON memory_crossrefs(target_id);

CREATE INDEX idx_crossrefs_similarity ON memory_crossrefs(similarity DESC);

CREATE TABLE worktrees (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    branch_name TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    base_branch TEXT DEFAULT 'main',
    agent_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    status TEXT DEFAULT 'active',
    merge_state TEXT,
    merged_at TIMESTAMPTZ,
    cleanup_after TIMESTAMPTZ,
    workspace_role TEXT NOT NULL DEFAULT 'task',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_worktrees_project ON worktrees(project_id);

CREATE INDEX idx_worktrees_status ON worktrees(status);

CREATE INDEX idx_worktrees_task ON worktrees(task_id);

CREATE INDEX idx_worktrees_session ON worktrees(agent_session_id);

CREATE UNIQUE INDEX idx_worktrees_branch ON worktrees(project_id, branch_name);

CREATE UNIQUE INDEX idx_worktrees_path ON worktrees(worktree_path);

CREATE TABLE merge_resolutions (
    id TEXT PRIMARY KEY,
    worktree_id TEXT NOT NULL REFERENCES worktrees(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    source_branch TEXT NOT NULL,
    target_branch TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    tier_used TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_merge_resolutions_worktree ON merge_resolutions(worktree_id);

CREATE INDEX idx_merge_resolutions_status ON merge_resolutions(status);

CREATE INDEX idx_merge_resolutions_source_branch ON merge_resolutions(source_branch);

CREATE INDEX idx_merge_resolutions_target_branch ON merge_resolutions(target_branch);

CREATE TABLE merge_conflicts (
    id TEXT PRIMARY KEY,
    resolution_id TEXT NOT NULL REFERENCES merge_resolutions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    file_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    ours_content TEXT,
    theirs_content TEXT,
    resolved_content TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_merge_conflicts_resolution ON merge_conflicts(resolution_id);

CREATE INDEX idx_merge_conflicts_file_path ON merge_conflicts(file_path);

CREATE INDEX idx_merge_conflicts_status ON merge_conflicts(status);

CREATE TABLE inter_session_messages (
    id TEXT PRIMARY KEY,
    from_session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    to_session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    content TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    sent_at TIMESTAMPTZ NOT NULL,
    read_at TIMESTAMPTZ,
    message_type TEXT NOT NULL DEFAULT 'message',
    metadata_json JSONB,
    delivered_at TIMESTAMPTZ
);

CREATE INDEX idx_inter_session_messages_from_session ON inter_session_messages(from_session);

CREATE INDEX idx_inter_session_messages_to_session ON inter_session_messages(to_session);

CREATE INDEX idx_inter_session_messages_unread ON inter_session_messages(to_session, read_at)
    WHERE read_at IS NULL;

CREATE INDEX idx_ism_undelivered ON inter_session_messages(to_session, delivered_at)
    WHERE delivered_at IS NULL;

CREATE INDEX idx_ism_completion_lookup ON inter_session_messages(to_session, message_type)
    WHERE metadata_json IS NOT NULL;

CREATE TABLE agent_commands (
    id TEXT PRIMARY KEY,
    from_session TEXT NOT NULL,
    to_session TEXT NOT NULL,
    command_text TEXT NOT NULL,
    allowed_tools JSONB,
    allowed_mcp_tools JSONB,
    exit_condition TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_agent_commands_to_session ON agent_commands(to_session, status);

CREATE TABLE skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    content TEXT NOT NULL,
    version TEXT,
    license TEXT,
    compatibility TEXT,
    allowed_tools JSONB,
    metadata JSONB,
    source_path TEXT,
    source_type TEXT,
    source_ref TEXT,
    hub_name TEXT,
    hub_slug TEXT,
    hub_version TEXT,
    enabled BOOLEAN DEFAULT TRUE,
    always_apply BOOLEAN DEFAULT FALSE,
    injection_format TEXT DEFAULT 'summary',
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    source TEXT DEFAULT 'installed',
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_skills_name ON skills(name);

CREATE INDEX idx_skills_project_id ON skills(project_id);

CREATE INDEX idx_skills_enabled ON skills(enabled);

CREATE INDEX idx_skills_always_apply ON skills(always_apply);

ALTER TABLE skills
    ADD CONSTRAINT idx_skills_name_project_source
    UNIQUE NULLS NOT DISTINCT (name, project_id, source);

CREATE INDEX idx_skills_deleted_at ON skills(deleted_at);

CREATE TABLE skill_files (
    id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    path TEXT NOT NULL,
    file_type TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(skill_id, path)
);

CREATE INDEX idx_skill_files_skill_id ON skill_files(skill_id);

CREATE INDEX idx_skill_files_type ON skill_files(file_type);

CREATE TABLE clones (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    branch_name TEXT NOT NULL,
    clone_path TEXT NOT NULL,
    base_branch TEXT DEFAULT 'main',
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    agent_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    status TEXT DEFAULT 'active',
    remote_url TEXT,
    last_sync_at TIMESTAMPTZ,
    cleanup_after TIMESTAMPTZ,
    workspace_role TEXT NOT NULL DEFAULT 'task',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_clones_project ON clones(project_id);

CREATE INDEX idx_clones_status ON clones(status);

CREATE INDEX idx_clones_task ON clones(task_id);

CREATE INDEX idx_clones_session ON clones(agent_session_id);

CREATE UNIQUE INDEX idx_clones_path ON clones(clone_path);

CREATE TABLE cron_jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    name TEXT NOT NULL,
    description TEXT,
    schedule_type TEXT NOT NULL,
    cron_expr TEXT,
    interval_seconds INTEGER,
    run_at TIMESTAMPTZ,
    timezone TEXT DEFAULT 'UTC',
    action_type TEXT NOT NULL,
    action_config JSONB NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    is_system BOOLEAN NOT NULL DEFAULT FALSE CHECK(is_system IN (FALSE, TRUE)),
    next_run_at TIMESTAMPTZ,
    last_run_at TIMESTAMPTZ,
    last_status TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_cron_jobs_project ON cron_jobs(project_id);

CREATE INDEX idx_cron_jobs_enabled ON cron_jobs(enabled);

CREATE INDEX idx_cron_jobs_next_run ON cron_jobs(next_run_at);

CREATE INDEX idx_cron_jobs_due ON cron_jobs(project_id, enabled, next_run_at);

CREATE TABLE cron_runs (
    id TEXT PRIMARY KEY,
    cron_job_id TEXT NOT NULL REFERENCES cron_jobs(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    triggered_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    status TEXT DEFAULT 'pending',
    output TEXT,
    error TEXT,
    agent_run_id TEXT,
    pipeline_execution_id TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_cron_runs_job ON cron_runs(cron_job_id);

CREATE INDEX idx_cron_runs_triggered ON cron_runs(triggered_at);

CREATE INDEX idx_cron_runs_status ON cron_runs(status);

CREATE TABLE project_github_triage_configs (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    enabled BOOLEAN NOT NULL DEFAULT FALSE CHECK (enabled IN (FALSE, TRUE)),
    webhook_enabled BOOLEAN NOT NULL DEFAULT FALSE CHECK (webhook_enabled IN (FALSE, TRUE)),
    repositories_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    reconcile_interval_seconds INTEGER NOT NULL DEFAULT 3600
        CHECK (reconcile_interval_seconds > 0),
    webhook_secret_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE gh_triage_deliveries (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    delivery_id TEXT NOT NULL,
    event TEXT NOT NULL,
    action TEXT,
    repository TEXT,
    issue_number INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'processed', 'ignored', 'duplicate', 'error')),
    payload_hash TEXT NOT NULL,
    headers_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_body TEXT NOT NULL DEFAULT '',
    error TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(project_id, delivery_id)
);

CREATE INDEX idx_gh_triage_deliveries_project_status
    ON gh_triage_deliveries(project_id, status);

CREATE INDEX idx_gh_triage_deliveries_issue
    ON gh_triage_deliveries(project_id, repository, issue_number);

CREATE TABLE gh_issues_triaged (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    issue_url TEXT,
    issue_state TEXT,
    labels_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    issue_updated_at TIMESTAMPTZ,
    content_hash TEXT NOT NULL,
    verdict TEXT NOT NULL
        CHECK (verdict IN ('implement', 'skip', 'escalate', 'dedup')),
    decision_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    vector_point_id TEXT,
    dedup_issue_key TEXT,
    source TEXT NOT NULL,
    last_triaged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(project_id, repo, issue_number)
);

CREATE INDEX idx_gh_issues_triaged_project_hash
    ON gh_issues_triaged(project_id, content_hash);

CREATE INDEX idx_gh_issues_triaged_task
    ON gh_issues_triaged(task_id);

CREATE TABLE pipeline_executions (
    id TEXT PRIMARY KEY,
    pipeline_name TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    status TEXT NOT NULL DEFAULT 'pending',
    inputs_json JSONB,
    outputs_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    resume_token TEXT UNIQUE,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    parent_execution_id TEXT REFERENCES pipeline_executions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    continuation_prompt TEXT,
    definition_json JSONB,
    review_json JSONB
);

CREATE INDEX idx_pipeline_executions_project ON pipeline_executions(project_id);

CREATE INDEX idx_pipeline_executions_status ON pipeline_executions(status);

CREATE INDEX idx_pipeline_executions_resume_token ON pipeline_executions(resume_token);

CREATE INDEX idx_pe_status_updated ON pipeline_executions(status, updated_at);

CREATE INDEX idx_pe_status_project_updated ON pipeline_executions(status, project_id, updated_at);

CREATE TABLE step_executions (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES pipeline_executions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    step_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    input_json JSONB,
    output_json JSONB,
    error TEXT,
    approval_token TEXT UNIQUE,
    approved_by TEXT,
    approved_at TIMESTAMPTZ,
    approval_timeout_seconds INTEGER,
    UNIQUE(execution_id, step_id)
);

CREATE INDEX idx_step_executions_execution ON step_executions(execution_id);

CREATE INDEX idx_step_executions_approval_token ON step_executions(approval_token);

CREATE TABLE secrets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    encrypted_value TEXT NOT NULL,
    category TEXT DEFAULT 'general',
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_secrets_category ON secrets(category);

CREATE TABLE task_comments (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    parent_comment_id TEXT REFERENCES task_comments(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    author TEXT NOT NULL,
    author_type TEXT NOT NULL DEFAULT 'session',
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_task_comments_task ON task_comments(task_id);

CREATE INDEX idx_task_comments_parent ON task_comments(parent_comment_id);

CREATE INDEX idx_task_comments_created ON task_comments(task_id, created_at);

CREATE TABLE session_skills (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    skill_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_session_skills_session ON session_skills(session_id);

CREATE UNIQUE INDEX idx_session_skills_unique ON session_skills(session_id, skill_name);

CREATE TABLE config_store (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'user',
    is_secret BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_config_store_source ON config_store(source);

CREATE TABLE workflow_definitions (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    name TEXT NOT NULL,
    description TEXT,
    workflow_type TEXT NOT NULL DEFAULT 'workflow',
    version TEXT DEFAULT '1.0',
    enabled BOOLEAN DEFAULT TRUE,
    priority INTEGER DEFAULT 100,
    sources JSONB,
    definition_json JSONB NOT NULL,
    canvas_json JSONB,
    source TEXT DEFAULT 'installed',
    tags JSONB,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_wf_defs_project ON workflow_definitions(project_id);

CREATE INDEX idx_wf_defs_name ON workflow_definitions(name);

CREATE INDEX idx_wf_defs_type ON workflow_definitions(workflow_type);

CREATE INDEX idx_wf_defs_enabled ON workflow_definitions(enabled);

ALTER TABLE workflow_definitions
    ADD CONSTRAINT idx_wf_defs_name_project
    UNIQUE NULLS NOT DISTINCT (name, project_id, source);

CREATE TABLE rule_overrides (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(session_id, rule_name)
);

CREATE TABLE prompts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    version TEXT DEFAULT '1.0',
    variables JSONB,
    scope TEXT NOT NULL DEFAULT 'bundled'
        CHECK(scope IN ('bundled', 'global', 'project')),
    source_path TEXT,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_prompts_name ON prompts(name);

CREATE INDEX idx_prompts_scope ON prompts(scope);

CREATE INDEX idx_prompts_project ON prompts(project_id);

ALTER TABLE prompts
    ADD CONSTRAINT idx_prompts_name_scope_project
    UNIQUE NULLS NOT DISTINCT (name, scope, project_id);

CREATE TABLE auth_sessions (
    token_hash TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    remember_me BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_auth_sessions_expires ON auth_sessions(expires_at);

CREATE TABLE model_costs (
    model TEXT PRIMARY KEY,
    provider TEXT,
    context_length INTEGER,
    max_completion_tokens INTEGER,
    source TEXT NOT NULL DEFAULT 'registry',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE savings_ledger (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id TEXT,
    project_id TEXT,
    category TEXT NOT NULL,
    original_tokens INTEGER NOT NULL,
    actual_tokens INTEGER NOT NULL,
    tokens_saved INTEGER NOT NULL,
    model TEXT,
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_savings_ledger_created ON savings_ledger(created_at);

CREATE INDEX idx_savings_ledger_project_cat ON savings_ledger(project_id, category);

CREATE TABLE token_events (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
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
    event_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB
);

CREATE INDEX idx_token_events_event_at ON token_events(event_at);

CREATE INDEX idx_token_events_session ON token_events(session_id, event_at);

CREATE INDEX idx_token_events_project_event ON token_events(project_id, event_at);

CREATE INDEX idx_token_events_model_family ON token_events(model_family, event_at);

CREATE UNIQUE INDEX idx_token_events_dedup
    ON token_events(session_id, message_id)
    WHERE message_id IS NOT NULL;

CREATE TABLE task_affected_files (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    file_path TEXT NOT NULL,
    annotation_source TEXT NOT NULL DEFAULT 'expansion',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(task_id, file_path)
);

CREATE INDEX idx_taf_task_id ON task_affected_files(task_id);

CREATE INDEX idx_taf_file_path ON task_affected_files(file_path);

CREATE TABLE completion_subscribers (
    completion_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    PRIMARY KEY (completion_id, session_id)
);

CREATE INDEX idx_completion_subscribers_completion ON completion_subscribers(completion_id);

CREATE TABLE code_indexed_projects (
    id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL,
    total_files INTEGER NOT NULL DEFAULT 0,
    total_symbols INTEGER NOT NULL DEFAULT 0,
    last_indexed_at TIMESTAMPTZ,
    index_duration_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE code_indexed_files (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    language TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    symbol_count INTEGER NOT NULL DEFAULT 0,
    byte_size INTEGER NOT NULL DEFAULT 0,
    graph_synced BOOLEAN NOT NULL DEFAULT FALSE,
    vectors_synced BOOLEAN NOT NULL DEFAULT FALSE,
    graph_sync_attempted_at TIMESTAMPTZ,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(project_id, file_path)
);

CREATE INDEX idx_cif_project ON code_indexed_files(project_id);

CREATE INDEX idx_cif_graph_synced ON code_indexed_files(project_id, graph_synced);

CREATE INDEX idx_cif_vectors_synced ON code_indexed_files(project_id, vectors_synced);

CREATE TABLE code_symbols (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    language TEXT NOT NULL,
    byte_start INTEGER NOT NULL,
    byte_end INTEGER NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    signature TEXT,
    docstring TEXT,
    parent_symbol_id TEXT,
    content_hash TEXT NOT NULL,
    summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_cs_project ON code_symbols(project_id);

CREATE INDEX idx_cs_file ON code_symbols(project_id, file_path);

CREATE INDEX idx_cs_name ON code_symbols(name);

CREATE INDEX idx_cs_qualified ON code_symbols(qualified_name);

CREATE INDEX idx_cs_kind ON code_symbols(kind);

CREATE INDEX idx_cs_parent ON code_symbols(parent_symbol_id);

CREATE TABLE code_imports (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id TEXT NOT NULL,
    source_file TEXT NOT NULL,
    target_module TEXT NOT NULL,
    UNIQUE(project_id, source_file, target_module)
);

CREATE INDEX idx_ci_file ON code_imports(project_id, source_file);

CREATE TABLE code_calls (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
);

CREATE INDEX idx_cc_file ON code_calls(project_id, file_path);

CREATE INDEX idx_cc_caller ON code_calls(project_id, caller_symbol_id);

CREATE INDEX idx_cc_target ON code_calls(project_id, callee_target_kind, callee_symbol_id, callee_name);

CREATE TABLE code_content_chunks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    content TEXT NOT NULL,
    language TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(project_id, file_path, chunk_index)
);

CREATE INDEX idx_ccc_project ON code_content_chunks(project_id);

CREATE INDEX idx_ccc_file ON code_content_chunks(project_id, file_path);

CREATE TABLE spans (
    span_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    parent_span_id TEXT,
    name TEXT NOT NULL,
    kind TEXT,
    start_time_ns BIGINT NOT NULL,
    end_time_ns BIGINT,
    status TEXT,
    status_message TEXT,
    attributes_json JSONB,
    events_json JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_spans_trace_id ON spans(trace_id);

CREATE INDEX idx_spans_start_time ON spans(start_time_ns);

CREATE TABLE metric_snapshots (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metrics_json JSONB NOT NULL
);

CREATE INDEX idx_metric_snapshots_ts ON metric_snapshots(timestamp);

CREATE TABLE bin_update_state (
    tool_name TEXT PRIMARY KEY,
    installed_version TEXT,
    floor_version TEXT NOT NULL,
    latest_version TEXT,
    binary_path TEXT,
    target TEXT,
    last_status TEXT NOT NULL CHECK (
        last_status IN (
            'updated',
            'up_to_date',
            'failed',
            'floor_violated',
            'dev',
            'source_unavailable'
        )
    ),
    last_error TEXT,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    installed_at TIMESTAMPTZ,
    source_url TEXT,
    is_dev BOOLEAN NOT NULL DEFAULT FALSE CHECK (is_dev IN (FALSE, TRUE)),
    floor_drift BOOLEAN NOT NULL DEFAULT FALSE CHECK (floor_drift IN (FALSE, TRUE)),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE comms_channels (
    id TEXT PRIMARY KEY,
    channel_type TEXT NOT NULL,
    name TEXT NOT NULL UNIQUE,
    enabled BOOLEAN DEFAULT TRUE,
    config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    webhook_secret TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE comms_identities (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES comms_channels(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    external_user_id TEXT NOT NULL,
    external_username TEXT,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(channel_id, external_user_id)
);

CREATE INDEX idx_comms_identities_channel ON comms_identities(channel_id);

CREATE INDEX idx_comms_identities_external_user ON comms_identities(external_user_id);

CREATE INDEX idx_comms_identities_session ON comms_identities(session_id);

CREATE TABLE comms_messages (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES comms_channels(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    identity_id TEXT REFERENCES comms_identities(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
    content TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'text',
    platform_message_id TEXT,
    platform_thread_id TEXT,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    status TEXT NOT NULL DEFAULT 'sent',
    error TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_comms_messages_channel_created ON comms_messages(channel_id, created_at);

CREATE INDEX idx_comms_messages_session ON comms_messages(session_id);

CREATE INDEX idx_comms_messages_direction ON comms_messages(direction);

CREATE TABLE comms_routing_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    channel_id TEXT REFERENCES comms_channels(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    event_pattern TEXT NOT NULL DEFAULT '*',
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    priority INTEGER DEFAULT 0,
    enabled BOOLEAN DEFAULT TRUE,
    config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_comms_routing_rules_channel ON comms_routing_rules(channel_id);

CREATE INDEX idx_comms_routing_rules_enabled ON comms_routing_rules(enabled);

CREATE TABLE comms_attachments (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES comms_messages(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    local_path TEXT,
    platform_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_comms_attachments_message ON comms_attachments(message_id);

CREATE TABLE metrics_events (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type TEXT NOT NULL,
    project_id TEXT,
    session_id TEXT,
    server_name TEXT,
    name TEXT NOT NULL,
    success BOOLEAN NOT NULL DEFAULT TRUE,
    latency_ms REAL,
    result TEXT,
    metadata_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_me_type_created ON metrics_events(event_type, created_at);

CREATE INDEX idx_me_session ON metrics_events(session_id, created_at);

CREATE INDEX idx_me_name ON metrics_events(name, event_type);

CREATE INDEX idx_me_created ON metrics_events(created_at);

CREATE TABLE metrics_events_archive (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '',
    server_name TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    call_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    total_latency_ms REAL NOT NULL DEFAULT 0,
    block_count INTEGER NOT NULL DEFAULT 0,
    allow_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(event_type, project_id, server_name, name)
);

CREATE TABLE chat_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    tool_calls_json JSONB,
    content_blocks_json JSONB,
    metadata_json JSONB,
    seq INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_chat_messages_conv_seq ON chat_messages(conversation_id, seq);

CREATE TABLE chat_attachments (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    -- Client/display identifiers intentionally do not reference server tables.
    draft_id TEXT,
    conversation_id TEXT,
    message_id TEXT,
    target_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    local_path TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    bound_at TIMESTAMPTZ -- Set once when an attachment is first bound to a message/session.
);

CREATE INDEX idx_chat_attachments_project ON chat_attachments(project_id);

CREATE INDEX idx_chat_attachments_draft ON chat_attachments(draft_id);

CREATE INDEX idx_chat_attachments_conversation ON chat_attachments(conversation_id);

CREATE INDEX idx_chat_attachments_message ON chat_attachments(message_id);

CREATE INDEX idx_chat_attachments_target_session ON chat_attachments(target_session_id);

CREATE INDEX idx_chat_attachments_local_path ON chat_attachments(local_path);

CREATE FUNCTION enforce_chat_attachments_bound_at_write_once()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.bound_at IS NOT NULL AND NEW.bound_at IS DISTINCT FROM OLD.bound_at THEN
        RAISE EXCEPTION 'chat_attachments.bound_at is write-once';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_chat_attachments_bound_at_write_once
BEFORE UPDATE OF bound_at ON chat_attachments
FOR EACH ROW
EXECUTE FUNCTION enforce_chat_attachments_bound_at_write_once();

CREATE FUNCTION touch_chat_attachments_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at THEN
        NEW.updated_at := NOW();
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_chat_attachments_updated_at_touch
BEFORE UPDATE ON chat_attachments
FOR EACH ROW
EXECUTE FUNCTION touch_chat_attachments_updated_at();

CREATE TABLE checkpoints (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    ref_name TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    parent_sha TEXT NOT NULL,
    files_changed INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT 'auto-checkpoint',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_checkpoints_task ON checkpoints(task_id, created_at DESC);

CREATE INDEX idx_checkpoints_session ON checkpoints(session_id);

CREATE INDEX idx_checkpoints_run ON checkpoints(run_id);

CREATE TABLE pending_interactions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    kind TEXT NOT NULL,
    provider TEXT NOT NULL,
    tool_name TEXT,
    payload_json JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    decision TEXT,
    response_json JSONB,
    timeout_seconds INTEGER NOT NULL DEFAULT 300,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

CREATE INDEX idx_pending_interactions_session ON pending_interactions(session_id, status);

CREATE UNIQUE INDEX idx_pending_interactions_active
    ON pending_interactions(session_id, kind)
    WHERE status = 'pending';

CREATE TABLE task_artifacts (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
            plan_file_path TEXT,
            plan_file_hash TEXT,
            worktree_path TEXT,
            worktree_id TEXT,
            clone_path TEXT,
            clone_id TEXT,
            base_commit_sha TEXT,
            target_branch TEXT,
            integration_branch TEXT,
            integration_workspace_id TEXT,
            integration_clone_id TEXT,
            expansion_run_id TEXT,
            expansion_attempts INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_reviewed_plan_hash TEXT, plan_review_attempts INTEGER NOT NULL DEFAULT 0, qa_attempts INTEGER NOT NULL DEFAULT 0, holistic_attempts INTEGER NOT NULL DEFAULT 0, merge_attempts INTEGER NOT NULL DEFAULT 0,
            CHECK (
                (worktree_path IS NULL) = (worktree_id IS NULL)
                AND (clone_path IS NULL) = (clone_id IS NULL)
                AND (worktree_path IS NULL OR clone_path IS NULL)
                AND (integration_workspace_id IS NULL OR integration_clone_id IS NULL)
                AND (
                    integration_workspace_id IS NULL
                    OR integration_branch IS NOT NULL
                )
                AND (
                    integration_clone_id IS NULL
                    OR integration_branch IS NOT NULL
                )
                AND (
                    base_commit_sha IS NULL
                    OR worktree_path IS NOT NULL
                    OR clone_path IS NOT NULL
                )
            )
        );

CREATE TABLE integration_workspace_mutex (
    integration_key TEXT PRIMARY KEY,
    lease_until TIMESTAMPTZ,
    lease_holder TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE task_delivery_campaigns (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    state TEXT NOT NULL DEFAULT 'pending',
    delivery_mode TEXT NOT NULL DEFAULT 'auto'
        CHECK (delivery_mode IN ('auto','pull_request')),
    source_repo TEXT,
    target_repo TEXT,
    merge_strategy TEXT NOT NULL DEFAULT 'squash'
        CHECK (merge_strategy IN ('merge', 'squash', 'rebase')),
    structured_pr_verdict JSONB,
    pr_report_ref TEXT,
    merge_sha TEXT,
    merge_report_ref TEXT,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE task_delivery_units (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    unit_key TEXT NOT NULL,
    worktree_id TEXT,
    repo TEXT,
    source_branch TEXT,
    target_branch TEXT NOT NULL DEFAULT 'main',
    pr_required BOOLEAN CHECK (pr_required IN (FALSE, TRUE)),
    protection_json JSONB,
    pr_url TEXT,
    github_pr_number INTEGER,
    gate_snapshot_json JSONB,
    pr_state TEXT,
    local_update_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(task_id, unit_key)
);

CREATE INDEX idx_task_delivery_units_task_id
    ON task_delivery_units(task_id);

CREATE INDEX idx_task_delivery_units_pr_url
    ON task_delivery_units(pr_url);

CREATE INDEX idx_pipeline_executions_created_at
            ON pipeline_executions (created_at DESC);

CREATE TABLE task_stages_registry (
                name TEXT PRIMARY KEY,
                display_label TEXT NOT NULL,
                description TEXT NOT NULL,
                category TEXT NOT NULL
                    CHECK (category IN ('discovery','design','verification','implementation','delivery')),
                default_agent TEXT,
                reviewer_agent TEXT,
                reviewer_agent_selector_json JSONB,
                review_policy TEXT NOT NULL DEFAULT 'none'
                    CHECK (review_policy IN ('none','required','optional')),
                dispatch_type TEXT
                    CHECK (dispatch_type IS NULL OR dispatch_type IN ('agent','pipeline')),
                dispatch_target TEXT,
                dispatch_inputs_json JSONB,
                position_hint INTEGER NOT NULL,
                requires_human BOOLEAN NOT NULL DEFAULT FALSE,
                is_terminal BOOLEAN NOT NULL DEFAULT FALSE,
                default_max_work_attempts INTEGER NOT NULL DEFAULT 3,
                default_max_review_rounds INTEGER NOT NULL DEFAULT 5,
                bundled_hash TEXT,
                deleted_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

CREATE INDEX idx_task_stages_registry_deleted
                ON task_stages_registry (deleted_at);

CREATE TABLE task_type_default_stages (
                task_type TEXT NOT NULL,
                stage_name TEXT NOT NULL
                    REFERENCES task_stages_registry(name) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
                position INTEGER NOT NULL,
                PRIMARY KEY (task_type, stage_name)
            );

CREATE INDEX idx_task_type_default_stages_position
                ON task_type_default_stages (task_type, position);

CREATE TABLE build_profiles (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                display_label TEXT NOT NULL,
                description TEXT NOT NULL,
                skip_stages_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                isolation TEXT NOT NULL DEFAULT 'worktree'
                    CHECK (isolation IN ('none','worktree','clone')),
                unattended BOOLEAN NOT NULL DEFAULT FALSE CHECK (unattended IN (FALSE, TRUE)),
                delivery_mode TEXT NOT NULL DEFAULT 'auto'
                    CHECK (delivery_mode IN ('auto','pull_request')),
                delivery_target_repo TEXT,
                enabled BOOLEAN NOT NULL DEFAULT TRUE CHECK (enabled IN (FALSE, TRUE)),
                source TEXT NOT NULL CHECK (source IN ('installed','project')),
                project_id TEXT REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
                tags_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                bundled_hash TEXT,
                deleted_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CHECK (source <> 'installed' OR project_id IS NULL)
            );

CREATE UNIQUE INDEX idx_build_profiles_active_unique
                ON build_profiles (name, project_id, source) NULLS NOT DISTINCT
                WHERE deleted_at IS NULL;

CREATE INDEX idx_build_profiles_project_source
                ON build_profiles (project_id, source, name);

CREATE TABLE task_stage_states (
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
                stage_name TEXT NOT NULL
                    REFERENCES task_stages_registry(name) ON DELETE RESTRICT DEFERRABLE INITIALLY IMMEDIATE,
                position INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'ready'
                    CHECK (
                        state IN ('ready','in_progress','done')
                        OR state IN ('needs_review','review_approved')
                    ),
                review_policy TEXT NOT NULL DEFAULT 'none'
                    CHECK (review_policy IN ('none','required','optional')),
                reviewer_agent TEXT,
                entered_at TIMESTAMPTZ,
                entered_by_session_id TEXT,
                completed_at TIMESTAMPTZ,
                completed_by_session_id TEXT,
                completed_commit_sha TEXT,
                work_attempt_count INTEGER NOT NULL DEFAULT 0,
                review_round_count INTEGER NOT NULL DEFAULT 0,
                max_work_attempts INTEGER,
                max_review_rounds INTEGER,
                artifact_refs JSONB,
                notes TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (task_id, stage_name)
            );

CREATE UNIQUE INDEX idx_task_stage_states_position
                ON task_stage_states (task_id, position);

CREATE INDEX idx_task_stage_states_state
                ON task_stage_states (stage_name, state);

CREATE INDEX idx_task_stage_states_open
                ON task_stage_states (task_id, position) WHERE state <> 'done';

CREATE INDEX idx_tasks_dispatch_scan
                ON tasks(allow_automation, closed_at, is_escalated);

CREATE INDEX idx_tasks_state_bucket
                ON tasks(state_bucket);

-- State bucket precedence is canonical: closed -> escalated -> first non-done stage -> ready.
CREATE FUNCTION compute_task_state_bucket(p_task_id TEXT)
RETURNS TEXT
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN t.closed_at IS NOT NULL THEN 'closed'
        WHEN t.escalated_at IS NOT NULL OR COALESCE(t.is_escalated, FALSE) IS TRUE THEN 'escalated'
        ELSE COALESCE(
            (
                SELECT CASE
                    WHEN stage_scan.state IN (
                        'ready', 'in_progress', 'needs_review', 'review_approved'
                    )
                    THEN stage_scan.state
                    ELSE 'ready'
                END
                  FROM task_stage_states stage_scan
                 WHERE stage_scan.task_id = p_task_id
                   AND stage_scan.state <> 'done'
                 ORDER BY stage_scan.position
                 LIMIT 1
            ),
            'ready'
        )
    END
    FROM tasks t
    WHERE t.id = p_task_id
$$;

CREATE FUNCTION refresh_task_state_bucket(p_task_id TEXT)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE tasks
       SET state_bucket = compute_task_state_bucket(p_task_id)
     WHERE id = p_task_id;
END;
$$;

CREATE FUNCTION refresh_task_state_bucket_from_task()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM refresh_task_state_bucket(NEW.id);
    RETURN NEW;
END;
$$;

CREATE FUNCTION refresh_task_state_bucket_from_stage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM refresh_task_state_bucket(OLD.task_id);
        RETURN OLD;
    END IF;

    PERFORM refresh_task_state_bucket(NEW.task_id);
    IF TG_OP = 'UPDATE' AND OLD.task_id IS DISTINCT FROM NEW.task_id THEN
        PERFORM refresh_task_state_bucket(OLD.task_id);
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER tasks_state_bucket_ai
AFTER INSERT ON tasks
FOR EACH ROW
EXECUTE FUNCTION refresh_task_state_bucket_from_task();

CREATE TRIGGER tasks_state_bucket_au
AFTER UPDATE OF closed_at, escalated_at, is_escalated ON tasks
FOR EACH ROW
EXECUTE FUNCTION refresh_task_state_bucket_from_task();

CREATE TRIGGER task_stage_states_state_bucket_ai
AFTER INSERT ON task_stage_states
FOR EACH ROW
EXECUTE FUNCTION refresh_task_state_bucket_from_stage();

CREATE TRIGGER task_stage_states_state_bucket_au
AFTER UPDATE OF state, position ON task_stage_states
FOR EACH ROW
EXECUTE FUNCTION refresh_task_state_bucket_from_stage();

CREATE TRIGGER task_stage_states_state_bucket_ad
AFTER DELETE ON task_stage_states
FOR EACH ROW
EXECUTE FUNCTION refresh_task_state_bucket_from_stage();

-- pg_search extension is provisioned by install (Docker initdb / native installer),
-- not by this schema. The runner probes for its presence and refuses to baseline
-- without it. See docs/runbooks/postgres-pgsearch-install.md.
CREATE INDEX tasks_search_bm25 ON tasks
USING bm25 (id, title, description)
WITH (key_field='id');

CREATE INDEX memories_search_bm25 ON memories
USING bm25 (id, content, tags_text)
WITH (key_field='id');

CREATE INDEX code_symbols_search_bm25 ON code_symbols
USING bm25 (id, name, qualified_name, signature, docstring, summary)
WITH (key_field='id');

CREATE INDEX code_content_search_bm25 ON code_content_chunks
USING bm25 (id, content)
WITH (key_field='id');

CREATE INDEX skills_search_bm25 ON skills
USING bm25 (id, name, description, content)
WITH (key_field='id');

-- Seed rows for projects

INSERT INTO "projects" ("id", "name", "repo_path", "github_url", "github_repo", "linear_team_id", "linear_project_id", "linear_synced_at", "deleted_at", "created_at", "updated_at") VALUES ('00000000-0000-0000-0000-000000000000', '_orphaned', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NOW(), NOW());
INSERT INTO "projects" ("id", "name", "repo_path", "github_url", "github_repo", "linear_team_id", "linear_project_id", "linear_synced_at", "deleted_at", "created_at", "updated_at") VALUES ('00000000-0000-0000-0000-000000000001', '_migrated', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NOW(), NOW());
INSERT INTO "projects" ("id", "name", "repo_path", "github_url", "github_repo", "linear_team_id", "linear_project_id", "linear_synced_at", "deleted_at", "created_at", "updated_at") VALUES ('00000000-0000-0000-0000-000000000002', '_global', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NOW(), NOW());
INSERT INTO "projects" ("id", "name", "repo_path", "github_url", "github_repo", "linear_team_id", "linear_project_id", "linear_synced_at", "deleted_at", "created_at", "updated_at") VALUES ('00000000-0000-0000-0000-000000060887', '_personal', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NOW(), NOW());

-- Seed rows for sessions
INSERT INTO "sessions" ("id", "external_id", "machine_id", "source", "project_id", "title", "title_source", "status", "transcript_path", "summary_path", "summary_markdown", "git_branch", "parent_session_id", "transcript_processed", "agent_depth", "spawned_by_agent_id", "workflow_name", "agent_run_id", "context_injected", "original_prompt", "usage_input_tokens", "usage_output_tokens", "usage_cache_creation_tokens", "usage_cache_read_tokens", "context_window", "terminal_context", "seq_num", "model", "is_local", "had_edits", "digest_markdown", "last_turn_markdown", "chat_mode", "last_digest_input_hash", "message_count", "turn_count", "tool_call_count", "last_assistant_content", "approved_tools_json", "session_type", "sandbox_enabled", "sandbox_policy_hash", "created_at", "updated_at") VALUES ('00000000-0000-0000-0000-000000000001', 'system', 'system', 'system', '00000000-0000-0000-0000-000000060887', '_system', NULL, 'active', NULL, NULL, NULL, NULL, NULL, FALSE, 0, NULL, NULL, NULL, FALSE, NULL, 0, 0, 0, 0, NULL, NULL, NULL, NULL, FALSE, FALSE, NULL, NULL, 'plan', NULL, 0, 0, 0, NULL, NULL, 'terminal', FALSE, NULL, NOW(), NOW());

-- Seed rows for task_stages_registry
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('ideation', 'Ideation', 'Early problem framing; capture motivating questions and constraints.', 'discovery', 'analyst', NULL, NULL, 'none', NULL, NULL, NULL, 10, FALSE, FALSE, 3, 5, '30d0d059953b56f2cf9e809b42993be29df0da15598a38925b79a900a71e6331', NOW());
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('research', 'Research', 'Targeted investigation; produce findings consumable by architecture/PRD.', 'discovery', 'researcher', NULL, NULL, 'none', NULL, NULL, NULL, 20, FALSE, FALSE, 3, 5, 'c18eb91008e5375fcc3395a220cf6bf7146cb5c1752f68daf848598a45857221', NOW());
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('architecture', 'Architecture', 'Cross-cutting design decisions and component shape.', 'design', 'architect', NULL, NULL, 'none', NULL, NULL, NULL, 30, FALSE, FALSE, 3, 5, 'd084b4acbf67c7012e577d2d386dc20ae45cbfebe347a58f3fbc89cef5038b2c', NOW());
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('prd', 'PRD', 'Productized requirements; bridges discovery and planning.', 'design', 'product-manager', NULL, NULL, 'none', NULL, NULL, NULL, 40, FALSE, FALSE, 3, 5, 'fd609d682a6fe7e807cfb487f301bfdb39f352bc8836b87e030bb0bbe7836360', NOW());
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('planning', 'Planning', 'Implementation plan authoring (interactive or autonomous).', 'design', 'planner', 'plan-adversary', NULL, 'required', NULL, NULL, NULL, 50, FALSE, FALSE, 3, 5, 'b7d0a297c57659700b759ce3f3fd6cc5e4d66e8a2a18759358ec683f613f2b51', NOW());
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('expansion', 'Expansion', 'Decompose plan into manifest-backed leaf tasks.', 'implementation', NULL, 'expansion-qa', NULL, 'required', 'pipeline', 'expand-task', '{"plan_file": "${{ artifacts.plan_file_path }}", "task_id": "${{ task_id }}"}', 80, FALSE, FALSE, 3, 5, '7aea4dbb7119bcdab1cb5957239670ff4a68d2d78d50c6e3a7bda922fa3d9aa1', NOW());
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('development', 'Development', 'Leaf implementation work; carries skill-backed TDD when required.', 'implementation', 'backend-developer', NULL, '{"default": "qa-reviewer", "rules": [{"category": "docs", "reviewer_agent": "doc-reviewer"}]}', 'required', NULL, NULL, NULL, 100, FALSE, FALSE, 3, 5, 'f8821338fc237ebc8abeb46a1e3303113e096593041a5dde768d0ff604221e54', NOW());
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('holistic_qa', 'Holistic QA', 'Whole-epic review after every leaf is parked.', 'verification', 'holistic-reviewer', 'holistic-reviewer', NULL, 'required', NULL, NULL, NULL, 120, FALSE, FALSE, 3, 5, '27acabfc718ad4f28be4bd3ae6d3bc10eb1602ddee65a6946dc05b093dfbc673', NOW());
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('pr', 'Pull Request', 'Open/update PR, capture verdict, gate on external review.', 'delivery', 'merge-orchestrator', NULL, NULL, 'required', NULL, NULL, NULL, 130, FALSE, FALSE, 3, 5, '38a13dbb652e4e1087abbfec0b97d1d4ac0161276183800e5e959a3a5d3b6cbb', NOW());
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('merge', 'Merge', 'Land approved PR; resolve conflicts; close terminal task.', 'delivery', 'merge-orchestrator', NULL, NULL, 'none', NULL, NULL, NULL, 140, FALSE, TRUE, 3, 5, '636a12f800c8ceef76dd7fdea41baaa0b227fa3f178bf45e3802688e179ec6ef', NOW());

-- Seed rows for task_type_default_stages
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('architecture_doc', 'research', 0);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('architecture_doc', 'architecture', 1);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('bug', 'development', 0);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('bug', 'pr', 1);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('bug', 'merge', 2);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('chore', 'development', 0);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('chore', 'pr', 1);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('chore', 'merge', 2);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('epic', 'ideation', 0);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('epic', 'research', 1);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('epic', 'architecture', 2);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('epic', 'prd', 3);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('epic', 'planning', 4);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('epic', 'expansion', 5);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('epic', 'development', 6);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('epic', 'holistic_qa', 7);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('epic', 'pr', 8);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('epic', 'merge', 9);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('feature', 'planning', 0);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('feature', 'expansion', 1);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('feature', 'development', 2);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('feature', 'pr', 3);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('feature', 'merge', 4);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('prd_doc', 'ideation', 0);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('prd_doc', 'prd', 1);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('refactor', 'planning', 0);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('refactor', 'development', 1);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('refactor', 'pr', 2);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('refactor', 'merge', 3);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('research_spike', 'ideation', 0);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('research_spike', 'research', 1);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('research_spike', 'prd', 2);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('review_anchor', 'planning', 0);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('simple_fix', 'development', 0);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('simple_fix', 'pr', 1);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('simple_fix', 'merge', 2);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('task', 'development', 0);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('task', 'pr', 1);
INSERT INTO "task_type_default_stages" ("task_type", "stage_name", "position") VALUES ('task', 'merge', 2);
