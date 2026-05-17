CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    repo_path TEXT,
    github_url TEXT,
    github_repo TEXT,
    linear_team_id TEXT,
    linear_project_id TEXT,
    linear_synced_at TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_projects_name ON projects(name);

CREATE TABLE mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    transport TEXT NOT NULL,
    url TEXT,
    command TEXT,
    args TEXT,
    env TEXT,
    headers TEXT,
    enabled INTEGER DEFAULT 1,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_mcp_servers_name ON mcp_servers(name);

CREATE INDEX idx_mcp_servers_project_id ON mcp_servers(project_id);

CREATE INDEX idx_mcp_servers_enabled ON mcp_servers(enabled);

CREATE UNIQUE INDEX idx_mcp_servers_name_project ON mcp_servers(name, project_id);

CREATE TABLE tools (
    id TEXT PRIMARY KEY,
    mcp_server_id TEXT NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    input_schema TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(mcp_server_id, name)
);

CREATE INDEX idx_tools_server_id ON tools(mcp_server_id);

CREATE INDEX idx_tools_name ON tools(name);

CREATE TABLE tool_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_id TEXT NOT NULL REFERENCES tools(id) ON DELETE CASCADE,
    server_name TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    embedding BLOB NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL,
    text_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(tool_id)
);

CREATE INDEX idx_tool_embeddings_tool ON tool_embeddings(tool_id);

CREATE INDEX idx_tool_embeddings_server ON tool_embeddings(server_name);

CREATE INDEX idx_tool_embeddings_project ON tool_embeddings(project_id);

CREATE INDEX idx_tool_embeddings_hash ON tool_embeddings(text_hash);

CREATE TABLE tool_schema_hashes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    project_id TEXT NOT NULL,
    schema_hash TEXT NOT NULL,
    last_verified_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, server_name, tool_name)
);

CREATE INDEX idx_schema_hashes_server ON tool_schema_hashes(server_name);

CREATE INDEX idx_schema_hashes_project ON tool_schema_hashes(project_id);

CREATE INDEX idx_schema_hashes_verified ON tool_schema_hashes(last_verified_at);

CREATE TABLE tool_metrics (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    server_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    call_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    total_latency_ms REAL NOT NULL DEFAULT 0,
    avg_latency_ms REAL,
    last_called_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, server_name, tool_name)
);

CREATE INDEX idx_tool_metrics_project ON tool_metrics(project_id);

CREATE INDEX idx_tool_metrics_server ON tool_metrics(server_name);

CREATE INDEX idx_tool_metrics_tool ON tool_metrics(tool_name);

CREATE INDEX idx_tool_metrics_call_count ON tool_metrics(call_count DESC);

CREATE INDEX idx_tool_metrics_last_called ON tool_metrics(last_called_at);

CREATE TABLE tool_metrics_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    server_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    date TEXT NOT NULL,
    call_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    total_latency_ms REAL NOT NULL DEFAULT 0,
    avg_latency_ms REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, server_name, tool_name, date)
);

CREATE INDEX idx_tool_metrics_daily_project ON tool_metrics_daily(project_id);

CREATE INDEX idx_tool_metrics_daily_date ON tool_metrics_daily(date);

CREATE INDEX idx_tool_metrics_daily_server ON tool_metrics_daily(server_name);

CREATE TABLE agent_runs (
    id TEXT PRIMARY KEY,
    parent_session_id TEXT NOT NULL REFERENCES sessions(id),
    child_session_id TEXT REFERENCES sessions(id),
    claimed_session_id TEXT REFERENCES sessions(id),
    workflow_name TEXT,
    agent_name TEXT,
    provider TEXT NOT NULL,
    model TEXT,
    is_local INTEGER NOT NULL DEFAULT 0,
    requested_reasoning_effort TEXT,
    effective_reasoning_effort TEXT,
    reasoning_required INTEGER NOT NULL DEFAULT 0,
    reasoning_status TEXT NOT NULL DEFAULT 'not_requested',
    reasoning_message TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    prompt TEXT NOT NULL,
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
    timeout_seconds REAL,
    terminal_reason TEXT
);

CREATE INDEX idx_agent_runs_parent_session ON agent_runs(parent_session_id);

CREATE INDEX idx_agent_runs_child_session ON agent_runs(child_session_id);

CREATE INDEX idx_agent_runs_status ON agent_runs(status);

CREATE INDEX idx_agent_runs_provider ON agent_runs(provider);

CREATE INDEX idx_agent_runs_task_id ON agent_runs(task_id);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    external_id TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    source TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id),
    title TEXT,
    title_source TEXT,
    status TEXT DEFAULT 'active',
    transcript_path TEXT,
    summary_path TEXT,
    summary_markdown TEXT,
    git_branch TEXT,
    parent_session_id TEXT REFERENCES sessions(id),
    transcript_processed BOOLEAN DEFAULT FALSE,
    agent_depth INTEGER DEFAULT 0,
    spawned_by_agent_id TEXT,
    workflow_name TEXT,
    agent_run_id TEXT REFERENCES agent_runs(id) ON DELETE SET NULL,
    context_injected INTEGER DEFAULT 0,
    original_prompt TEXT,
    usage_input_tokens INTEGER DEFAULT 0,
    usage_output_tokens INTEGER DEFAULT 0,
    usage_cache_creation_tokens INTEGER DEFAULT 0,
    usage_cache_read_tokens INTEGER DEFAULT 0,
    context_window INTEGER,
    terminal_context TEXT,
    seq_num INTEGER,
    model TEXT,
    is_local INTEGER NOT NULL DEFAULT 0,
    had_edits BOOLEAN DEFAULT 0,
    digest_markdown TEXT,
    last_turn_markdown TEXT,
    chat_mode TEXT DEFAULT 'plan',
    last_digest_input_hash TEXT,
    message_count INTEGER DEFAULT 0,
    turn_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    last_assistant_content TEXT,
    approved_tools_json TEXT,
    session_type TEXT NOT NULL DEFAULT 'terminal',
    sandbox_enabled BOOLEAN DEFAULT 0,
    sandbox_policy_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
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
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    reason TEXT,
    requested_at TEXT NOT NULL,
    acknowledged_at TEXT
);

CREATE INDEX idx_stop_signals_pending ON session_stop_signals(acknowledged_at)
    WHERE acknowledged_at IS NULL;

CREATE TABLE loop_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    progress_type TEXT NOT NULL,
    tool_name TEXT,
    details TEXT,
    recorded_at TEXT NOT NULL,
    is_high_value INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_loop_progress_session ON loop_progress(session_id, recorded_at DESC);

CREATE INDEX idx_loop_progress_high_value ON loop_progress(session_id, is_high_value, recorded_at DESC)
    WHERE is_high_value = 1;

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    parent_task_id TEXT REFERENCES tasks(id),
    created_in_session_id TEXT REFERENCES sessions(id),
    claimed_by_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    closed_in_session_id TEXT REFERENCES sessions(id),
    closed_commit_sha TEXT,
    closed_at TEXT,
    title TEXT NOT NULL,
    description TEXT,
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
    allow_automation INTEGER NOT NULL DEFAULT 0 CHECK(allow_automation IN (0, 1)),
    unattended INTEGER NOT NULL DEFAULT 0 CHECK(unattended IN (0, 1)),
    isolation TEXT NOT NULL DEFAULT 'worktree' CHECK(isolation IN ('none', 'worktree', 'clone')),
    assigned_agent TEXT,
    additional_skills TEXT,
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
, is_escalated INTEGER NOT NULL DEFAULT 0
                    CHECK(is_escalated IN (0, 1)),
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

CREATE TABLE plans (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    plan_id TEXT NOT NULL,
    plan_path TEXT NOT NULL,
    plan_hash TEXT,
    plan_kind TEXT NOT NULL CHECK(plan_kind IN ('implementation', 'strategy')),
    state TEXT NOT NULL CHECK(state IN ('active', 'archived')),
    root_task_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    UNIQUE (project_id, plan_id)
);

CREATE INDEX idx_plans_root_task ON plans(root_task_ref);

CREATE INDEX idx_plans_state ON plans(state);

CREATE INDEX idx_plans_project_state ON plans(project_id, state);

CREATE TABLE task_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    dep_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, depends_on, dep_type)
);

CREATE INDEX idx_deps_task ON task_dependencies(task_id);

CREATE INDEX idx_deps_depends_on ON task_dependencies(depends_on);

CREATE TABLE task_dispatch_mutex (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    lease_until TEXT,
    lease_holder TEXT,
    run_id TEXT,
    action_kind TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_dispatch_mutex_scan ON task_dispatch_mutex(lease_until, run_id);
CREATE INDEX idx_dispatch_mutex_run_id ON task_dispatch_mutex(run_id);

CREATE TABLE task_lifecycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    by_actor TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_lifecycle_events_task ON task_lifecycle_events(task_id, created_at);

CREATE TABLE project_lifecycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    event TEXT NOT NULL,
    reason TEXT NOT NULL,
    by_actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_project_lifecycle_events_project
    ON project_lifecycle_events(project_id, created_at);

CREATE TABLE expansion_runs (
    id TEXT PRIMARY KEY,
    parent_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    triggering_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'running', 'compiled', 'applying', 'completed', 'failed', 'cancelled')),
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
);

CREATE INDEX idx_expansion_runs_parent_task ON expansion_runs(parent_task_id, created_at DESC);

CREATE INDEX idx_expansion_runs_status ON expansion_runs(status, created_at DESC);

CREATE TABLE session_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, task_id, action)
);

CREATE INDEX idx_session_tasks_session ON session_tasks(session_id);

CREATE INDEX idx_session_tasks_task ON session_tasks(task_id);

CREATE TABLE task_validation_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    iteration INTEGER NOT NULL,
    status TEXT NOT NULL,
    feedback TEXT,
    issues TEXT,
    context_type TEXT,
    context_summary TEXT,
    validator_type TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_validation_history_task ON task_validation_history(task_id);

CREATE TABLE task_selection_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    selected_at TEXT NOT NULL,
    context TEXT
);

CREATE INDEX idx_task_selection_session ON task_selection_history(session_id, selected_at DESC);

CREATE INDEX idx_task_selection_task ON task_selection_history(session_id, task_id, selected_at DESC);

CREATE TABLE workflow_states (
    session_id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    step TEXT NOT NULL,
    step_entered_at TEXT,
    step_action_count INTEGER DEFAULT 0,
    total_action_count INTEGER DEFAULT 0,
    context_injected INTEGER DEFAULT 0,
    variables TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE workflow_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    step TEXT NOT NULL,
    event_type TEXT NOT NULL,
    tool_name TEXT,
    rule_id TEXT,
    condition TEXT,
    result TEXT NOT NULL,
    reason TEXT,
    context TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX idx_audit_session ON workflow_audit_log(session_id);

CREATE INDEX idx_audit_timestamp ON workflow_audit_log(timestamp);

CREATE INDEX idx_audit_event_type ON workflow_audit_log(event_type);

CREATE INDEX idx_audit_result ON workflow_audit_log(result);

CREATE TABLE workflow_instances (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    workflow_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 100,
    current_step TEXT,
    step_entered_at TEXT,
    step_action_count INTEGER DEFAULT 0,
    total_action_count INTEGER DEFAULT 0,
    variables TEXT DEFAULT '{}',
    context_injected INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session_id, workflow_name),
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX idx_workflow_instances_session ON workflow_instances(session_id);

CREATE INDEX idx_workflow_instances_enabled ON workflow_instances(session_id, enabled);

CREATE TABLE session_variables (
    session_id TEXT PRIMARY KEY,
    variables TEXT DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id),
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    source_type TEXT,
    source_session_id TEXT REFERENCES sessions(id),
    access_count INTEGER DEFAULT 0,
    last_accessed_at TEXT,
    tags TEXT,
    media TEXT,
    graph_processed INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_memories_project ON memories(project_id);

CREATE INDEX idx_memories_type ON memories(memory_type);

CREATE INDEX idx_memories_graph_pending ON memories(graph_processed) WHERE graph_processed = 0;

CREATE INDEX idx_memories_source_session ON memories(source_session_id);

CREATE TABLE session_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, memory_id, action)
);

CREATE INDEX idx_session_memories_session ON session_memories(session_id);

CREATE INDEX idx_session_memories_memory ON session_memories(memory_id);

CREATE TABLE memory_crossrefs (
    source_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    similarity REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source_id, target_id)
);

CREATE INDEX idx_crossrefs_source ON memory_crossrefs(source_id);

CREATE INDEX idx_crossrefs_target ON memory_crossrefs(target_id);

CREATE INDEX idx_crossrefs_similarity ON memory_crossrefs(similarity DESC);

CREATE TABLE worktrees (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    branch_name TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    base_branch TEXT DEFAULT 'main',
    agent_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    status TEXT DEFAULT 'active',
    merge_state TEXT,
    merged_at TEXT,
    cleanup_after TEXT,
    workspace_role TEXT NOT NULL DEFAULT 'task',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_worktrees_project ON worktrees(project_id);

CREATE INDEX idx_worktrees_status ON worktrees(status);

CREATE INDEX idx_worktrees_task ON worktrees(task_id);

CREATE INDEX idx_worktrees_session ON worktrees(agent_session_id);

CREATE UNIQUE INDEX idx_worktrees_branch ON worktrees(project_id, branch_name);

CREATE UNIQUE INDEX idx_worktrees_path ON worktrees(worktree_path);

CREATE TABLE merge_resolutions (
    id TEXT PRIMARY KEY,
    worktree_id TEXT NOT NULL REFERENCES worktrees(id) ON DELETE CASCADE,
    source_branch TEXT NOT NULL,
    target_branch TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    tier_used TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_merge_resolutions_worktree ON merge_resolutions(worktree_id);

CREATE INDEX idx_merge_resolutions_status ON merge_resolutions(status);

CREATE INDEX idx_merge_resolutions_source_branch ON merge_resolutions(source_branch);

CREATE INDEX idx_merge_resolutions_target_branch ON merge_resolutions(target_branch);

CREATE TABLE merge_conflicts (
    id TEXT PRIMARY KEY,
    resolution_id TEXT NOT NULL REFERENCES merge_resolutions(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    ours_content TEXT,
    theirs_content TEXT,
    resolved_content TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_merge_conflicts_resolution ON merge_conflicts(resolution_id);

CREATE INDEX idx_merge_conflicts_file_path ON merge_conflicts(file_path);

CREATE INDEX idx_merge_conflicts_status ON merge_conflicts(status);

CREATE TABLE inter_session_messages (
    id TEXT PRIMARY KEY,
    from_session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    to_session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    sent_at TEXT NOT NULL,
    read_at TEXT,
    message_type TEXT NOT NULL DEFAULT 'message',
    metadata_json TEXT,
    delivered_at TEXT
);

CREATE INDEX idx_inter_session_messages_from_session ON inter_session_messages(from_session);

CREATE INDEX idx_inter_session_messages_to_session ON inter_session_messages(to_session);

CREATE INDEX idx_inter_session_messages_unread ON inter_session_messages(to_session, read_at)
    WHERE read_at IS NULL;

CREATE INDEX idx_ism_undelivered ON inter_session_messages(to_session, delivered_at)
    WHERE delivered_at IS NULL;

CREATE TABLE agent_commands (
    id TEXT PRIMARY KEY,
    from_session TEXT NOT NULL,
    to_session TEXT NOT NULL,
    command_text TEXT NOT NULL,
    allowed_tools TEXT,
    allowed_mcp_tools TEXT,
    exit_condition TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT
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
    allowed_tools TEXT,
    metadata TEXT,
    source_path TEXT,
    source_type TEXT,
    source_ref TEXT,
    hub_name TEXT,
    hub_slug TEXT,
    hub_version TEXT,
    enabled INTEGER DEFAULT 1,
    always_apply INTEGER DEFAULT 0,
    injection_format TEXT DEFAULT 'summary',
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    source TEXT DEFAULT 'installed',
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_skills_name ON skills(name);

CREATE INDEX idx_skills_project_id ON skills(project_id);

CREATE INDEX idx_skills_enabled ON skills(enabled);

CREATE INDEX idx_skills_always_apply ON skills(always_apply);

CREATE UNIQUE INDEX idx_skills_name_project_source
    ON skills(name, COALESCE(project_id, '__global__'), source);

CREATE INDEX idx_skills_deleted_at ON skills(deleted_at);

CREATE TABLE skill_files (
    id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    file_type TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(skill_id, path)
);

CREATE INDEX idx_skill_files_skill_id ON skill_files(skill_id);

CREATE INDEX idx_skill_files_type ON skill_files(file_type);

CREATE TABLE clones (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    branch_name TEXT NOT NULL,
    clone_path TEXT NOT NULL,
    base_branch TEXT DEFAULT 'main',
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    agent_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    status TEXT DEFAULT 'active',
    remote_url TEXT,
    last_sync_at TEXT,
    cleanup_after TEXT,
    workspace_role TEXT NOT NULL DEFAULT 'task',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_clones_project ON clones(project_id);

CREATE INDEX idx_clones_status ON clones(status);

CREATE INDEX idx_clones_task ON clones(task_id);

CREATE INDEX idx_clones_session ON clones(agent_session_id);

CREATE UNIQUE INDEX idx_clones_path ON clones(clone_path);

CREATE TABLE cron_jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    schedule_type TEXT NOT NULL,
    cron_expr TEXT,
    interval_seconds INTEGER,
    run_at TEXT,
    timezone TEXT DEFAULT 'UTC',
    action_type TEXT NOT NULL,
    action_config TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    is_system INTEGER NOT NULL DEFAULT 0 CHECK(is_system IN (0, 1)),
    next_run_at TEXT,
    last_run_at TEXT,
    last_status TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_cron_jobs_project ON cron_jobs(project_id);

CREATE INDEX idx_cron_jobs_enabled ON cron_jobs(enabled);

CREATE INDEX idx_cron_jobs_next_run ON cron_jobs(next_run_at);

CREATE INDEX idx_cron_jobs_due ON cron_jobs(project_id, enabled, next_run_at);

CREATE TABLE cron_runs (
    id TEXT PRIMARY KEY,
    cron_job_id TEXT NOT NULL REFERENCES cron_jobs(id) ON DELETE CASCADE,
    triggered_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    status TEXT DEFAULT 'pending',
    output TEXT,
    error TEXT,
    agent_run_id TEXT,
    pipeline_execution_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_cron_runs_job ON cron_runs(cron_job_id);

CREATE INDEX idx_cron_runs_triggered ON cron_runs(triggered_at);

CREATE INDEX idx_cron_runs_status ON cron_runs(status);

CREATE TABLE project_github_triage_configs (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    webhook_enabled INTEGER NOT NULL DEFAULT 0 CHECK (webhook_enabled IN (0, 1)),
    repositories_json TEXT NOT NULL DEFAULT '[]',
    reconcile_interval_seconds INTEGER NOT NULL DEFAULT 3600
        CHECK (reconcile_interval_seconds > 0),
    webhook_secret_ref TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE gh_triage_deliveries (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    delivery_id TEXT NOT NULL,
    event TEXT NOT NULL,
    action TEXT,
    repository TEXT,
    issue_number INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'processed', 'ignored', 'duplicate', 'error')),
    payload_hash TEXT NOT NULL,
    headers_json TEXT NOT NULL DEFAULT '{}',
    raw_body TEXT NOT NULL DEFAULT '',
    error TEXT,
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, delivery_id)
);

CREATE INDEX idx_gh_triage_deliveries_project_status
    ON gh_triage_deliveries(project_id, status);

CREATE INDEX idx_gh_triage_deliveries_issue
    ON gh_triage_deliveries(project_id, repository, issue_number);

CREATE TABLE gh_issues_triaged (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    issue_url TEXT,
    issue_state TEXT,
    labels_json TEXT NOT NULL DEFAULT '[]',
    issue_updated_at TEXT,
    content_hash TEXT NOT NULL,
    verdict TEXT NOT NULL
        CHECK (verdict IN ('implement', 'skip', 'escalate', 'dedup')),
    decision_json TEXT NOT NULL DEFAULT '{}',
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    vector_point_id TEXT,
    dedup_issue_key TEXT,
    source TEXT NOT NULL,
    last_triaged_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, repo, issue_number)
);

CREATE INDEX idx_gh_issues_triaged_project_hash
    ON gh_issues_triaged(project_id, content_hash);

CREATE INDEX idx_gh_issues_triaged_task
    ON gh_issues_triaged(task_id);

CREATE TABLE pipeline_executions (
    id TEXT PRIMARY KEY,
    pipeline_name TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    inputs_json TEXT,
    outputs_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    resume_token TEXT UNIQUE,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    parent_execution_id TEXT REFERENCES pipeline_executions(id) ON DELETE CASCADE,
    continuation_prompt TEXT,
    definition_json TEXT,
    review_json TEXT
);

CREATE INDEX idx_pipeline_executions_project ON pipeline_executions(project_id);

CREATE INDEX idx_pipeline_executions_status ON pipeline_executions(status);

CREATE INDEX idx_pipeline_executions_resume_token ON pipeline_executions(resume_token);

CREATE INDEX idx_pe_status_updated ON pipeline_executions(status, updated_at);

CREATE INDEX idx_pe_status_project_updated ON pipeline_executions(status, project_id, updated_at);

CREATE TABLE step_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL REFERENCES pipeline_executions(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TEXT,
    completed_at TEXT,
    input_json TEXT,
    output_json TEXT,
    error TEXT,
    approval_token TEXT UNIQUE,
    approved_by TEXT,
    approved_at TEXT,
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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_secrets_category ON secrets(category);

CREATE TABLE task_comments (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    parent_comment_id TEXT REFERENCES task_comments(id) ON DELETE CASCADE,
    author TEXT NOT NULL,
    author_type TEXT NOT NULL DEFAULT 'session',
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_task_comments_task ON task_comments(task_id);

CREATE INDEX idx_task_comments_parent ON task_comments(parent_comment_id);

CREATE INDEX idx_task_comments_created ON task_comments(task_id, created_at);

CREATE TABLE session_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    skill_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_session_skills_session ON session_skills(session_id);

CREATE UNIQUE INDEX idx_session_skills_unique ON session_skills(session_id, skill_name);

CREATE TABLE config_store (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'user',
    is_secret INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_config_store_source ON config_store(source);

CREATE TABLE workflow_definitions (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    workflow_type TEXT NOT NULL DEFAULT 'workflow',
    version TEXT DEFAULT '1.0',
    enabled INTEGER DEFAULT 1,
    priority INTEGER DEFAULT 100,
    sources TEXT,
    definition_json TEXT NOT NULL,
    canvas_json TEXT,
    source TEXT DEFAULT 'installed',
    tags TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_wf_defs_project ON workflow_definitions(project_id);

CREATE INDEX idx_wf_defs_name ON workflow_definitions(name);

CREATE INDEX idx_wf_defs_type ON workflow_definitions(workflow_type);

CREATE INDEX idx_wf_defs_enabled ON workflow_definitions(enabled);

CREATE UNIQUE INDEX idx_wf_defs_name_project ON workflow_definitions(name, COALESCE(project_id, '__global__'), source);

CREATE TABLE rule_overrides (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session_id, rule_name)
);

CREATE TABLE prompts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    version TEXT DEFAULT '1.0',
    variables TEXT,
    scope TEXT NOT NULL DEFAULT 'bundled'
        CHECK(scope IN ('bundled', 'global', 'project')),
    source_path TEXT,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_prompts_name ON prompts(name);

CREATE INDEX idx_prompts_scope ON prompts(scope);

CREATE INDEX idx_prompts_project ON prompts(project_id);

CREATE UNIQUE INDEX idx_prompts_name_scope_project
    ON prompts(name, scope, COALESCE(project_id, ''));

CREATE TABLE auth_sessions (
    token TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    remember_me INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_auth_sessions_expires ON auth_sessions(expires_at);

CREATE TABLE model_costs (
    model TEXT PRIMARY KEY,
    provider TEXT,
    context_length INTEGER,
    max_completion_tokens INTEGER,
    source TEXT NOT NULL DEFAULT 'registry',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE savings_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    project_id TEXT,
    category TEXT NOT NULL,
    original_tokens INTEGER NOT NULL,
    actual_tokens INTEGER NOT NULL,
    tokens_saved INTEGER NOT NULL,
    model TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_savings_ledger_created ON savings_ledger(created_at);

CREATE INDEX idx_savings_ledger_project_cat ON savings_ledger(project_id, category);

CREATE TABLE token_events (
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
);

CREATE INDEX idx_token_events_event_at ON token_events(event_at);

CREATE INDEX idx_token_events_session ON token_events(session_id, event_at);

CREATE INDEX idx_token_events_project_event ON token_events(project_id, event_at);

CREATE INDEX idx_token_events_model_family ON token_events(model_family, event_at);

CREATE UNIQUE INDEX idx_token_events_dedup
    ON token_events(session_id, message_id)
    WHERE message_id IS NOT NULL;

CREATE TABLE task_affected_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    annotation_source TEXT NOT NULL DEFAULT 'expansion',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
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
    last_indexed_at TEXT,
    index_duration_ms INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE code_indexed_files (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    language TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    symbol_count INTEGER NOT NULL DEFAULT 0,
    byte_size INTEGER NOT NULL DEFAULT 0,
    graph_synced INTEGER NOT NULL DEFAULT 0,
    vectors_synced INTEGER NOT NULL DEFAULT 0,
    graph_sync_attempted_at TEXT,
    indexed_at TEXT NOT NULL DEFAULT (datetime('now')),
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
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_cs_project ON code_symbols(project_id);

CREATE INDEX idx_cs_file ON code_symbols(project_id, file_path);

CREATE INDEX idx_cs_name ON code_symbols(name);

CREATE INDEX idx_cs_qualified ON code_symbols(qualified_name);

CREATE INDEX idx_cs_kind ON code_symbols(kind);

CREATE INDEX idx_cs_parent ON code_symbols(parent_symbol_id);

CREATE TABLE code_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    source_file TEXT NOT NULL,
    target_module TEXT NOT NULL,
    UNIQUE(project_id, source_file, target_module)
);

CREATE INDEX idx_ci_file ON code_imports(project_id, source_file);

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
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
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
    start_time_ns INTEGER NOT NULL,
    end_time_ns INTEGER,
    status TEXT,
    status_message TEXT,
    attributes_json TEXT,
    events_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_spans_trace_id ON spans(trace_id);

CREATE INDEX idx_spans_start_time ON spans(start_time_ns);

CREATE TABLE metric_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    metrics_json TEXT NOT NULL
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
    checked_at TEXT NOT NULL DEFAULT (datetime('now')),
    installed_at TEXT,
    source_url TEXT,
    is_dev INTEGER NOT NULL DEFAULT 0 CHECK (is_dev IN (0, 1)),
    floor_drift INTEGER NOT NULL DEFAULT 0 CHECK (floor_drift IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE comms_channels (
    id TEXT PRIMARY KEY,
    channel_type TEXT NOT NULL,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',
    webhook_secret TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE comms_identities (
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

CREATE INDEX idx_comms_identities_channel ON comms_identities(channel_id);

CREATE INDEX idx_comms_identities_external_user ON comms_identities(external_user_id);

CREATE INDEX idx_comms_identities_session ON comms_identities(session_id);

CREATE TABLE comms_messages (
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

CREATE INDEX idx_comms_messages_channel_created ON comms_messages(channel_id, created_at);

CREATE INDEX idx_comms_messages_session ON comms_messages(session_id);

CREATE INDEX idx_comms_messages_direction ON comms_messages(direction);

CREATE TABLE comms_routing_rules (
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

CREATE INDEX idx_comms_routing_rules_channel ON comms_routing_rules(channel_id);

CREATE INDEX idx_comms_routing_rules_enabled ON comms_routing_rules(enabled);

CREATE TABLE comms_attachments (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES comms_messages(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    local_path TEXT,
    platform_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_comms_attachments_message ON comms_attachments(message_id);

CREATE TABLE metrics_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    project_id TEXT,
    session_id TEXT,
    server_name TEXT,
    name TEXT NOT NULL,
    success INTEGER NOT NULL DEFAULT 1,
    latency_ms REAL,
    result TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now'))
);

CREATE INDEX idx_me_type_created ON metrics_events(event_type, created_at);

CREATE INDEX idx_me_session ON metrics_events(session_id, created_at);

CREATE INDEX idx_me_name ON metrics_events(name, event_type);

CREATE INDEX idx_me_created ON metrics_events(created_at);

CREATE TABLE metrics_events_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    tool_calls_json TEXT,
    content_blocks_json TEXT,
    metadata_json TEXT,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_chat_messages_conv_seq ON chat_messages(conversation_id, seq);

CREATE TABLE chat_attachments (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    -- Client/display identifiers intentionally do not reference server tables.
    draft_id TEXT,
    conversation_id TEXT,
    message_id TEXT,
    target_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    local_path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    bound_at TEXT
);

CREATE INDEX idx_chat_attachments_project ON chat_attachments(project_id);

CREATE INDEX idx_chat_attachments_draft ON chat_attachments(draft_id);

CREATE INDEX idx_chat_attachments_conversation ON chat_attachments(conversation_id);

CREATE INDEX idx_chat_attachments_message ON chat_attachments(message_id);

CREATE INDEX idx_chat_attachments_target_session ON chat_attachments(target_session_id);

CREATE TABLE checkpoints (
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

CREATE INDEX idx_checkpoints_task ON checkpoints(task_id, created_at DESC);

CREATE INDEX idx_checkpoints_session ON checkpoints(session_id);

CREATE INDEX idx_checkpoints_run ON checkpoints(run_id);

CREATE TABLE pending_interactions (
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

CREATE INDEX idx_pending_interactions_session ON pending_interactions(session_id, status);

CREATE UNIQUE INDEX idx_pending_interactions_active
    ON pending_interactions(session_id, kind)
    WHERE status = 'pending';

CREATE TABLE task_artifacts (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
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
            updated_at TEXT NOT NULL DEFAULT (datetime('now')), last_reviewed_plan_hash TEXT, plan_review_attempts INTEGER NOT NULL DEFAULT 0, qa_attempts INTEGER NOT NULL DEFAULT 0, holistic_attempts INTEGER NOT NULL DEFAULT 0, merge_attempts INTEGER NOT NULL DEFAULT 0,
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
    lease_until TEXT,
    lease_holder TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE task_delivery_campaigns (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'pending',
    delivery_mode TEXT NOT NULL DEFAULT 'auto'
        CHECK (delivery_mode IN ('auto','pull_request')),
    source_repo TEXT,
    target_repo TEXT,
    merge_strategy TEXT NOT NULL DEFAULT 'squash'
        CHECK (merge_strategy IN ('merge', 'squash', 'rebase')),
    structured_pr_verdict TEXT,
    pr_report_ref TEXT,
    merge_sha TEXT,
    merge_report_ref TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE task_delivery_units (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    unit_key TEXT NOT NULL,
    worktree_id TEXT,
    repo TEXT,
    source_branch TEXT,
    target_branch TEXT NOT NULL DEFAULT 'main',
    pr_required INTEGER CHECK (pr_required IN (0, 1)),
    protection_json TEXT,
    pr_url TEXT,
    github_pr_number INTEGER,
    gate_snapshot_json TEXT,
    pr_state TEXT,
    local_update_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
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
                reviewer_agent_selector_json TEXT,
                review_policy TEXT NOT NULL DEFAULT 'none'
                    CHECK (review_policy IN ('none','required','optional')),
                dispatch_type TEXT
                    CHECK (dispatch_type IS NULL OR dispatch_type IN ('agent','pipeline')),
                dispatch_target TEXT,
                dispatch_inputs_json TEXT,
                position_hint INTEGER NOT NULL,
                requires_human INTEGER NOT NULL DEFAULT 0,
                is_terminal INTEGER NOT NULL DEFAULT 0,
                default_max_work_attempts INTEGER NOT NULL DEFAULT 3,
                default_max_review_rounds INTEGER NOT NULL DEFAULT 5,
                bundled_hash TEXT,
                deleted_at TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

CREATE INDEX idx_task_stages_registry_deleted
                ON task_stages_registry (deleted_at);

CREATE TABLE task_type_default_stages (
                task_type TEXT NOT NULL,
                stage_name TEXT NOT NULL
                    REFERENCES task_stages_registry(name) ON DELETE CASCADE,
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
                skip_stages_json TEXT NOT NULL DEFAULT '[]',
                isolation TEXT NOT NULL DEFAULT 'worktree'
                    CHECK (isolation IN ('none','worktree','clone')),
                unattended INTEGER NOT NULL DEFAULT 0 CHECK (unattended IN (0, 1)),
                delivery_mode TEXT NOT NULL DEFAULT 'auto'
                    CHECK (delivery_mode IN ('auto','pull_request')),
                delivery_target_repo TEXT,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                source TEXT NOT NULL CHECK (source IN ('installed','project')),
                project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
                tags_json TEXT NOT NULL DEFAULT '[]',
                bundled_hash TEXT,
                deleted_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                CHECK (source != 'installed' OR project_id IS NULL)
            );

CREATE UNIQUE INDEX idx_build_profiles_active_unique
                ON build_profiles (name, COALESCE(project_id, '__global__'), source)
                WHERE deleted_at IS NULL;

CREATE INDEX idx_build_profiles_project_source
                ON build_profiles (project_id, source, name);

CREATE TABLE task_stage_states (
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                stage_name TEXT NOT NULL
                    REFERENCES task_stages_registry(name) ON DELETE RESTRICT,
                position INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'ready'
                    CHECK (
                        state IN ('ready','in_progress','done')
                        OR state IN ('needs_review','review_approved')
                    ),
                review_policy TEXT NOT NULL DEFAULT 'none'
                    CHECK (review_policy IN ('none','required','optional')),
                reviewer_agent TEXT,
                entered_at TEXT,
                entered_by_session_id TEXT,
                completed_at TEXT,
                completed_by_session_id TEXT,
                completed_commit_sha TEXT,
                work_attempt_count INTEGER NOT NULL DEFAULT 0,
                review_round_count INTEGER NOT NULL DEFAULT 0,
                max_work_attempts INTEGER,
                max_review_rounds INTEGER,
                artifact_refs TEXT,
                notes TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (task_id, stage_name)
            );

CREATE UNIQUE INDEX idx_task_stage_states_position
                ON task_stage_states (task_id, position);

CREATE INDEX idx_task_stage_states_state
                ON task_stage_states (stage_name, state);

CREATE INDEX idx_task_stage_states_open
                ON task_stage_states (task_id, position) WHERE state != 'done';

CREATE INDEX idx_tasks_dispatch_scan
                ON tasks(allow_automation, closed_at, is_escalated);

CREATE INDEX idx_tasks_state_bucket
                ON tasks(state_bucket);

-- State bucket precedence is canonical: closed -> escalated -> first non-done stage -> ready.
CREATE TRIGGER tasks_state_bucket_ai
        AFTER INSERT ON tasks
        BEGIN
            UPDATE tasks
               SET state_bucket = CASE
                    WHEN closed_at IS NOT NULL THEN 'closed'
                    WHEN escalated_at IS NOT NULL OR COALESCE(is_escalated, 0) = 1 THEN 'escalated'
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
                             WHERE stage_scan.task_id = NEW.id
                               AND stage_scan.state != 'done'
                             ORDER BY stage_scan.position
                             LIMIT 1
                        ),
                        'ready'
                    )
                END
             WHERE id = NEW.id;
        END;

CREATE TRIGGER tasks_state_bucket_au
        AFTER UPDATE OF closed_at, escalated_at, is_escalated ON tasks
        BEGIN
            UPDATE tasks
               SET state_bucket = CASE
                    WHEN closed_at IS NOT NULL THEN 'closed'
                    WHEN escalated_at IS NOT NULL OR COALESCE(is_escalated, 0) = 1 THEN 'escalated'
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
                             WHERE stage_scan.task_id = NEW.id
                               AND stage_scan.state != 'done'
                             ORDER BY stage_scan.position
                             LIMIT 1
                        ),
                        'ready'
                    )
                END
             WHERE id = NEW.id;
        END;

CREATE TRIGGER task_stage_states_state_bucket_ai
        AFTER INSERT ON task_stage_states
        BEGIN
            UPDATE tasks
               SET state_bucket = CASE
                    WHEN closed_at IS NOT NULL THEN 'closed'
                    WHEN escalated_at IS NOT NULL OR COALESCE(is_escalated, 0) = 1 THEN 'escalated'
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
                             WHERE stage_scan.task_id = NEW.task_id
                               AND stage_scan.state != 'done'
                             ORDER BY stage_scan.position
                             LIMIT 1
                        ),
                        'ready'
                    )
                END
             WHERE id = NEW.task_id;
        END;

CREATE TRIGGER task_stage_states_state_bucket_au
        AFTER UPDATE OF state, position ON task_stage_states
        BEGIN
            UPDATE tasks
               SET state_bucket = CASE
                    WHEN closed_at IS NOT NULL THEN 'closed'
                    WHEN escalated_at IS NOT NULL OR COALESCE(is_escalated, 0) = 1 THEN 'escalated'
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
                             WHERE stage_scan.task_id = NEW.task_id
                               AND stage_scan.state != 'done'
                             ORDER BY stage_scan.position
                             LIMIT 1
                        ),
                        'ready'
                    )
                END
             WHERE id = NEW.task_id;

            UPDATE tasks
               SET state_bucket = CASE
                    WHEN closed_at IS NOT NULL THEN 'closed'
                    WHEN escalated_at IS NOT NULL OR COALESCE(is_escalated, 0) = 1 THEN 'escalated'
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
                             WHERE stage_scan.task_id = OLD.task_id
                               AND stage_scan.state != 'done'
                             ORDER BY stage_scan.position
                             LIMIT 1
                        ),
                        'ready'
                    )
                END
             WHERE id = OLD.task_id
               AND OLD.task_id != NEW.task_id;
        END;

CREATE TRIGGER task_stage_states_state_bucket_ad
        AFTER DELETE ON task_stage_states
        BEGIN
            UPDATE tasks
               SET state_bucket = CASE
                    WHEN closed_at IS NOT NULL THEN 'closed'
                    WHEN escalated_at IS NOT NULL OR COALESCE(is_escalated, 0) = 1 THEN 'escalated'
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
                             WHERE stage_scan.task_id = OLD.task_id
                               AND stage_scan.state != 'done'
                             ORDER BY stage_scan.position
                             LIMIT 1
                        ),
                        'ready'
                    )
                END
             WHERE id = OLD.task_id;
        END;

-- Seed rows for projects
INSERT INTO "projects" ("id", "name", "repo_path", "github_url", "github_repo", "linear_team_id", "linear_project_id", "linear_synced_at", "deleted_at", "created_at", "updated_at") VALUES ('00000000-0000-0000-0000-000000000000', '_orphaned', NULL, NULL, NULL, NULL, NULL, NULL, NULL, datetime('now'), datetime('now'));
INSERT INTO "projects" ("id", "name", "repo_path", "github_url", "github_repo", "linear_team_id", "linear_project_id", "linear_synced_at", "deleted_at", "created_at", "updated_at") VALUES ('00000000-0000-0000-0000-000000000001', '_migrated', NULL, NULL, NULL, NULL, NULL, NULL, NULL, datetime('now'), datetime('now'));
INSERT INTO "projects" ("id", "name", "repo_path", "github_url", "github_repo", "linear_team_id", "linear_project_id", "linear_synced_at", "deleted_at", "created_at", "updated_at") VALUES ('00000000-0000-0000-0000-000000000002', '_global', NULL, NULL, NULL, NULL, NULL, NULL, NULL, datetime('now'), datetime('now'));
INSERT INTO "projects" ("id", "name", "repo_path", "github_url", "github_repo", "linear_team_id", "linear_project_id", "linear_synced_at", "deleted_at", "created_at", "updated_at") VALUES ('00000000-0000-0000-0000-000000060887', '_personal', NULL, NULL, NULL, NULL, NULL, NULL, NULL, datetime('now'), datetime('now'));

-- Seed rows for sessions
INSERT INTO "sessions" ("id", "external_id", "machine_id", "source", "project_id", "title", "title_source", "status", "transcript_path", "summary_path", "summary_markdown", "git_branch", "parent_session_id", "transcript_processed", "agent_depth", "spawned_by_agent_id", "workflow_name", "agent_run_id", "context_injected", "original_prompt", "usage_input_tokens", "usage_output_tokens", "usage_cache_creation_tokens", "usage_cache_read_tokens", "context_window", "terminal_context", "seq_num", "model", "is_local", "had_edits", "digest_markdown", "last_turn_markdown", "chat_mode", "last_digest_input_hash", "message_count", "turn_count", "tool_call_count", "last_assistant_content", "approved_tools_json", "session_type", "sandbox_enabled", "sandbox_policy_hash", "created_at", "updated_at") VALUES ('00000000-0000-0000-0000-000000000001', 'system', 'system', 'system', '00000000-0000-0000-0000-000000060887', '_system', NULL, 'active', NULL, NULL, NULL, NULL, NULL, 0, 0, NULL, NULL, NULL, 0, NULL, 0, 0, 0, 0, NULL, NULL, NULL, NULL, 0, 0, NULL, NULL, 'plan', NULL, 0, 0, 0, NULL, NULL, 'terminal', 0, NULL, datetime('now'), datetime('now'));

-- Seed rows for task_stages_registry
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('ideation', 'Ideation', 'Early problem framing; capture motivating questions and constraints.', 'discovery', 'analyst', NULL, NULL, 'none', NULL, NULL, NULL, 10, 0, 0, 3, 5, '30d0d059953b56f2cf9e809b42993be29df0da15598a38925b79a900a71e6331', datetime('now'));
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('research', 'Research', 'Targeted investigation; produce findings consumable by architecture/PRD.', 'discovery', 'researcher', NULL, NULL, 'none', NULL, NULL, NULL, 20, 0, 0, 3, 5, 'c18eb91008e5375fcc3395a220cf6bf7146cb5c1752f68daf848598a45857221', datetime('now'));
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('architecture', 'Architecture', 'Cross-cutting design decisions and component shape.', 'design', 'architect', NULL, NULL, 'none', NULL, NULL, NULL, 30, 0, 0, 3, 5, 'd084b4acbf67c7012e577d2d386dc20ae45cbfebe347a58f3fbc89cef5038b2c', datetime('now'));
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('prd', 'PRD', 'Productized requirements; bridges discovery and planning.', 'design', 'product-manager', NULL, NULL, 'none', NULL, NULL, NULL, 40, 0, 0, 3, 5, 'fd609d682a6fe7e807cfb487f301bfdb39f352bc8836b87e030bb0bbe7836360', datetime('now'));
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('planning', 'Planning', 'Implementation plan authoring (interactive or autonomous).', 'design', 'planner', 'plan-adversary', NULL, 'required', NULL, NULL, NULL, 50, 0, 0, 3, 5, 'b7d0a297c57659700b759ce3f3fd6cc5e4d66e8a2a18759358ec683f613f2b51', datetime('now'));
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('expansion', 'Expansion', 'Decompose plan into TDD-wrapped leaf tasks.', 'implementation', NULL, 'expansion-qa', NULL, 'required', 'pipeline', 'expand-task', '{"plan_file": "${{ artifacts.plan_file_path }}", "task_id": "${{ task_id }}"}', 80, 0, 0, 3, 5, '7aea4dbb7119bcdab1cb5957239670ff4a68d2d78d50c6e3a7bda922fa3d9aa1', datetime('now'));
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('development', 'Development', 'Leaf implementation work; drives TDD sandwiches.', 'implementation', 'backend-developer', NULL, '{"default": "qa-reviewer", "rules": [{"category": "docs", "reviewer_agent": "doc-reviewer"}]}', 'required', NULL, NULL, NULL, 100, 0, 0, 3, 5, 'f8821338fc237ebc8abeb46a1e3303113e096593041a5dde768d0ff604221e54', datetime('now'));
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('holistic_qa', 'Holistic QA', 'Whole-epic review after every leaf is parked.', 'verification', 'holistic-reviewer', 'holistic-reviewer', NULL, 'required', NULL, NULL, NULL, 120, 0, 0, 3, 5, '27acabfc718ad4f28be4bd3ae6d3bc10eb1602ddee65a6946dc05b093dfbc673', datetime('now'));
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('pr', 'Pull Request', 'Open/update PR, capture verdict, gate on external review.', 'delivery', 'merge-orchestrator', NULL, NULL, 'required', NULL, NULL, NULL, 130, 0, 0, 3, 5, '38a13dbb652e4e1087abbfec0b97d1d4ac0161276183800e5e959a3a5d3b6cbb', datetime('now'));
INSERT INTO "task_stages_registry" ("name", "display_label", "description", "category", "default_agent", "reviewer_agent", "reviewer_agent_selector_json", "review_policy", "dispatch_type", "dispatch_target", "dispatch_inputs_json", "position_hint", "requires_human", "is_terminal", "default_max_work_attempts", "default_max_review_rounds", "bundled_hash", "updated_at") VALUES ('merge', 'Merge', 'Land approved PR; resolve conflicts; close terminal task.', 'delivery', 'merge-orchestrator', NULL, NULL, 'none', NULL, NULL, NULL, 140, 0, 1, 3, 5, '636a12f800c8ceef76dd7fdea41baaa0b227fa3f178bf45e3802688e179ec6ef', datetime('now'));

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
