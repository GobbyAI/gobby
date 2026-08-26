/**
 * Style-surface capture harness — the repeatable screenshot gate for the
 * Phase 2 styling consolidation (plan section 1.3). Chrome DevTools stays the
 * ad-hoc debugging tool; this spec is the evidence gate for every risky step.
 *
 * ## Opt-in execution (never part of a default run)
 *
 * Capture cells are tagged `@style-capture`: the default `chromium` project
 * excludes them, and the whole matrix is additionally gated on an explicit
 * capture-run id. To produce a run:
 *
 * ```sh
 * cd web
 * GOBBY_CAPTURE_RUN_ID="$(git rev-parse --short HEAD)-before" \
 *   npx playwright test tests/style-surfaces.spec.ts \
 *   --project=style-capture --project=style-capture-coarse
 * ```
 *
 * Two projects because the pointer axis is a LAUNCH-time property: the
 * runner bakes pointer/touch blink settings into the browser process from
 * each project's `use`, so fine and coarse cells need separate projects.
 *
 * Do NOT pass `--reporter` — a CLI reporter override replaces the config's
 * reporter list and silently drops the attestation reporter; the finalizer
 * will refuse the run. (`GOBBY_CAPTURE_BROWSER=<chromium executable>` is
 * available for machines without the Playwright browser bundle.)
 *
 * ## Before/after capture workflow
 *
 * 1. On the pre-change commit, run the command above with the label
 *    `<sha>-before`. The globalTeardown finalizer publishes
 *    `tests/screenshots/style-captures/runs/<sha>-before/` only when every
 *    expected matrix cell captured successfully.
 * 2. Apply the styling change (the "flip").
 * 3. Run again with `GOBBY_CAPTURE_RUN_ID="$(git rev-parse --short HEAD)-after"`.
 * 4. Compare pairs by identical file name across the two run directories —
 *    `<surface>--<state>--<theme>--<pointer>--<viewport>.png` is stable, so
 *    any image tool can diff run A against run B name-by-name. Review is
 *    human (no committed baselines, no pixel-diff gate, per plan decision).
 *
 * Runs are immutable: re-using a label refuses to overwrite the finalized
 * directory. Failed or partial runs never occupy their label and can simply
 * be re-run. See `tests/support/captureRunFinalizer.ts` for the full
 * finalizer contract (attempt-scoped staging, runner-final attestation,
 * exact key-set equality, atomic publish).
 *
 * ## Representative mappings (surfaces the matrix cannot photograph)
 *
 * Recorded in `REPRESENTATIVE_MAPPINGS` below with equivalence rationales,
 * and asserted against the live tree so they cannot silently rot.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import {
  expect,
  test,
  type Browser,
  type Locator,
  type Page,
  type TestInfo,
  type WebSocketRoute,
} from "@playwright/test";
import { ACTIVITY_PANEL_TABS } from "../src/components/activity/ActivityPanelTabs";
import { SETTINGS_SECTIONS } from "../src/components/settings/sections";
import {
  CAPTURE_RUN_ENV,
  buildCaptureScenarios,
  captureRootDir,
  expandCaptureCells,
  runDirFor,
  stageCaptureCell,
  type CaptureCell,
  type CaptureCellFragment,
  type FinalizedRunManifest,
} from "./support/captureRunFinalizer";

const MODULE_DIR = path.dirname(fileURLToPath(import.meta.url));
const SRC_DIR = path.resolve(MODULE_DIR, "..", "src");
const RUN_ID = process.env[CAPTURE_RUN_ENV];
const BASE_URL = process.env.PLAYWRIGHT_BASE_URL || "http://localhost:60889";

/** Frozen wall clock for every capture cell (relative dates stay stable). */
const FIXED_TIME = new Date("2026-04-20T12:00:00Z");

const PROJECT_ID = "proj-1";
const DB_SESSION_ID = "web-current";
// The proven web-chat seeding pattern (web-chat-restore-plan.spec.ts) keys
// BOTH `gobby-conversation-id` and `gobby-db-session-id` to the same value;
// conversation-scoped WS frames then address it directly.
const CONVERSATION_ID = DB_SESSION_ID;

// ---------------------------------------------------------------------------
// Seed data
// ---------------------------------------------------------------------------

const PROJECT = {
  id: PROJECT_ID,
  name: "project-one",
  display_name: "Project One",
  repo_path: "/tmp/project-one",
  github_url: null,
  github_repo: null,
  linear_team_id: null,
  linear_project_id: null,
  approval_rules: [],
  validation_detection: null,
  created_at: "2026-04-08T12:00:00Z",
  updated_at: "2026-04-08T12:00:00Z",
  session_count: 2,
  open_task_count: 1,
  last_activity_at: "2026-04-19T18:00:00Z",
};

function sessionRow(overrides: Record<string, unknown>) {
  return {
    id: "sess-a",
    ref: "#301",
    external_id: "sess-a-ext",
    source: "claude",
    project_id: PROJECT_ID,
    title: "Session A",
    status: "active",
    model: "sonnet",
    message_count: 2,
    created_at: "2026-04-08T12:00:00Z",
    updated_at: "2026-04-19T18:05:00Z",
    seq_num: 301,
    summary_markdown: null,
    digest_markdown: null,
    git_branch: "main",
    usage_input_tokens: 1200,
    usage_output_tokens: 480,
    had_edits: true,
    agent_depth: 0,
    chat_mode: "plan",
    parent_session_id: null,
    session_type: "web_chat",
    terminal_context: null,
    sandbox_enabled: true,
    sandbox_policy_hash: "policy-web",
    transcript_path: null,
    agent_run_id: null,
    claimed_task_refs: [],
    created_task_refs: [],
    closed_task_refs: [],
    ...overrides,
  };
}

/** The persisted current web-chat session — resolving it to a DIFFERENT
 * session would make the app adopt that session's external id and break
 * every conversation-scoped WS frame. */
const CURRENT_SESSION = sessionRow({
  id: DB_SESSION_ID,
  ref: "#300",
  external_id: `${DB_SESSION_ID}-ext`,
  title: "Current web chat",
  seq_num: 300,
  session_type: "web_chat",
});

const SESSIONS = [
  // Catalog rows are CLI sessions: an ACTIVE web_chat row would be
  // auto-adopted as the current conversation, replacing the seeded one and
  // detaching every conversation-scoped WS frame.
  sessionRow({ session_type: "cli" }),
  sessionRow({
    id: "sess-b",
    ref: "#302",
    external_id: "sess-b-ext",
    source: "codex",
    title: "Retire legacy settings",
    status: "paused",
    seq_num: 302,
    session_type: "cli",
    terminal_context: { tmux_session: "gobby-302" },
    sandbox_enabled: false,
  }),
];

const STAGE_ROW = {
  name: "development",
  display_name: "Development",
  display_label: "Development",
  description: "Implement the change",
  category: "development",
  state: "in_progress",
  review_policy: "required",
  default_agent: "builder",
  reviewer_agent: "reviewer",
  reviewer_agent_selector_json: null,
  dispatch_type: null,
  dispatch_target: null,
  dispatch_inputs_json: null,
  position: 1,
  position_hint: 1,
  sequence_order: 1,
  requires_human: false,
  is_terminal: false,
  default_max_work_attempts: 3,
  default_max_review_rounds: 2,
  deleted_at: null,
  is_edited: false,
  updated_at: "2026-04-19T18:30:00Z",
};

const PLANNING_STAGE = {
  ...STAGE_ROW,
  name: "planning",
  display_name: "Planning",
  display_label: "Planning",
  description: "Shape the work",
  state: "done",
  position: 0,
  position_hint: 0,
  sequence_order: 0,
};

const TASK = {
  id: "task-14425",
  ref: "#14425",
  title: "Verify web chat task state survives refresh",
  status: "in_progress",
  priority: 2,
  task_type: "bug",
  parent_task_id: null,
  created_at: "2026-04-19T18:30:00Z",
  updated_at: "2026-04-19T18:35:00Z",
  seq_num: 14425,
  path_cache: "14425",
  project_id: PROJECT_ID,
  claimed_by_session_id: DB_SESSION_ID,
  closed_at: null,
  escalated_at: null,
  current_stage: STAGE_ROW,
  stages: [PLANNING_STAGE, STAGE_ROW],
  state: {
    owner_session_id: DB_SESSION_ID,
    current_stage: STAGE_ROW,
    is_claimed: true,
    is_closed: false,
    is_escalated: false,
    is_blocked: false,
    is_merge_ready: false,
    closed_at: null,
    closed_reason: null,
    closed_in_session_id: null,
    closed_commit_sha: null,
    escalated_at: null,
    escalation_reason: null,
  },
};

const AGENT_DEFINITION = {
  definition: {
    name: "reviewer",
    description: "Reviews pull requests",
    surfaces: ["spawn"],
    role: "reviewer",
    goal: null,
    personality: null,
    instructions: null,
    provider: "claude",
    model: "opus",
    reasoning_effort: null,
    reasoning_required: false,
    fallback_agent: null,
    mode: "inherit",
    isolation: "worktree",
    base_branch: "inherit",
    timeout: 0,
    default_workflow: null,
    sandbox: null,
    workflows: { variables: { REVIEW_DEPTH: "high" } },
    lifecycle_variables: {},
    default_variables: {},
    step_workflow: {
      steps: [],
      variables: null,
      exit_condition: null,
    },
    blocked_tools: [],
    blocked_mcp_tools: [],
  },
  source: "installed",
  source_path: null,
  db_id: "agent-db-1",
  enabled: true,
  overridden_by: null,
  deleted_at: null,
  tags: ["review"],
};

const PROVIDER_CATALOG = {
  providers: [
    {
      provider: "claude",
      available: true,
      source: "static",
      models: [
        { value: "opus", label: "Claude Opus", is_default: true },
        { value: "sonnet", label: "Claude Sonnet" },
      ],
    },
    {
      provider: "codex",
      available: true,
      source: "static",
      models: [{ value: "gpt-5", label: "GPT-5" }],
    },
  ],
};

const SKILL = {
  id: "skill-1",
  name: "impeccable",
  description: "Frontend design review skill",
  content: "# Impeccable\n\nDesign guidance for the web surfaces.",
  version: "1.2.0",
  license: "MIT",
  compatibility: null,
  allowed_tools: [],
  metadata: { category: "Design" },
  source_path: "/tmp/skills/impeccable",
  source_type: "local",
  source_ref: null,
  source: "installed",
  hub_name: null,
  hub_slug: null,
  hub_version: null,
  enabled: true,
  always_apply: false,
  injection_format: "summary",
  project_id: null,
  deleted_at: null,
  created_at: "2026-04-08T12:00:00Z",
  updated_at: "2026-04-08T12:00:00Z",
};

const RULE = {
  id: "rule-1",
  name: "no-secrets-in-diff",
  description: "Block secrets in diffs",
  event: "before_tool",
  group: "safety",
  when: null,
  enabled: true,
  priority: 100,
  source: "project",
  tags: ["security"],
  effects: [{ type: "deny", message: "Secret detected" }],
  match: null,
  audience: "all",
  agent_scope: [],
  project_id: PROJECT_ID,
  has_template_update: false,
};

const MEMORY = {
  id: "mem-1",
  memory_type: "fact",
  content: "The activity panel is resizable and persists its width.",
  created_at: "2026-04-08T12:00:00Z",
  updated_at: "2026-04-08T12:00:00Z",
  project_id: PROJECT_ID,
  is_global: false,
  source_type: "session",
  source_session_id: "sess-a",
  importance: 0.8,
  access_count: 3,
  last_accessed_at: "2026-04-19T12:30:00Z",
  tags: ["ui", "panel"],
  deleted_at: null,
  dream_action: null,
  last_dreamed_at: null,
};

const VARIABLE_DEFINITION = {
  id: "var-1",
  name: "max_retries",
  description: "Default retry budget",
  kind: "variable",
  version: "1.0",
  enabled: true,
  source: "installed",
  tags: null,
  project_id: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  deleted_at: null,
  default_value: 3,
  value: 3,
};

const PIPELINE_DEFINITION = {
  id: "wf-1",
  name: "nightly-verify",
  description: "Nightly verification",
  kind: "pipeline",
  version: "1.0",
  enabled: true,
  source: "project",
  tags: ["ci"],
  project_id: PROJECT_ID,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  deleted_at: null,
  definition_json: '{"steps":[{"id":"lint"},{"id":"tests"}]}',
  canvas_json: null,
};

const CHAT_MESSAGES = [
  {
    id: "msg-1",
    role: "user",
    content: "Summarize the styling consolidation status.",
    tool_calls: [],
    seq: 1,
    created_at: "2026-04-20T11:58:00Z",
  },
  {
    id: "msg-2",
    role: "assistant",
    content: [
      "## Styling consolidation",
      "",
      "The sweep is **on track**. Key items:",
      "",
      "- Chip and Card primitives shipped",
      "- FormField consolidation next",
      "",
      "```ts",
      'export const tone = "supported" as const;',
      "```",
      "",
      "| Phase | State |",
      "| --- | --- |",
      "| P1 | done |",
      "| P4 | sweeping |",
    ].join("\n"),
    tool_calls: [],
    seq: 2,
    created_at: "2026-04-20T11:59:00Z",
  },
];

const LONG_LINE =
  "const veryLongIdentifierThatNeverWraps = configureStyleSurfaceCaptureHarness({ retainAspectRatio: true, grid: 'auto-fit', tokens: ['--space-sm', '--space-md', '--space-lg'] });";

const OVERFLOW_MESSAGES = [
  CHAT_MESSAGES[0],
  {
    ...CHAT_MESSAGES[1],
    id: "msg-overflow",
    content: [
      "Long-form content exercising mono overflow and scroll behavior.",
      "",
      "```ts",
      ...Array.from(
        { length: 40 },
        (_, i) => `const row${i} = ${i}; ${LONG_LINE}`,
      ),
      "```",
      "",
      Array.from(
        { length: 30 },
        (_, i) =>
          `Paragraph ${i}: the quick brown fox jumps over the lazy dog, at length, repeatedly, to force scrolling.`,
      ).join("\n\n"),
    ].join("\n"),
  },
];

const WIKI_LONG_CONTENT = [
  "---",
  "title: Home",
  "---",
  "",
  "# Home",
  "",
  ...Array.from(
    { length: 60 },
    (_, i) =>
      `Section ${i} body copy that keeps the reader scrolling through a realistically long wiki document.\n`,
  ),
].join("\n");

/** One nested config tree feeding /api/config/values for the 13 settings
 * sections, the memory tab's purge banner, and the knowledge-graph limits. */
const CONFIG_VALUES = {
  daemon_port: 8742,
  bind_host: "127.0.0.1",
  daemon_health_check_interval: 30,
  test_mode: false,
  cors_origins: ["http://localhost:5173", "https://app.example.com"],
  websocket: { enabled: true, port: 8743, ping_interval: 20, ping_timeout: 20 },
  ui: {
    enabled: true,
    mode: "auto",
    port: 60889,
    host: "localhost",
    web_dir: "",
    knowledge_graph_limit: 500,
    knowledge_graph_relationship_limit: 2000,
  },
  search: {
    mode: "hybrid",
    keyword_weight: 0.4,
    embedding_weight: 0.6,
    notify_on_fallback: true,
  },
  auth: { username: "admin", password: "********" },
  embeddings: {
    model: "text-embedding-3-small",
    dim: 1536,
    api_base: null,
    query_prefix: null,
    api_key: "********",
  },
  databases: {
    qdrant: {
      url: "http://localhost:6333",
      port: 6333,
      collection_prefix: "gobby_",
      api_key: "********",
    },
    falkordb: {
      host: "localhost",
      port: 6379,
      graph_name: "gobby",
      graph_search: true,
      graph_min_score: 0.35,
      rrf_k: 60,
      password: "********",
    },
  },
  tool_approval: {
    enabled: true,
    default_policy: "approve_once",
    policies: [
      {
        server_pattern: "gobby-tasks",
        tool_pattern: "create_task",
        policy: "always_ask",
      },
      { server_pattern: "*", tool_pattern: "Read", policy: "auto" },
    ],
  },
  chat: {
    profile: "feature_mid",
    default_mode: "normal",
    candidates: ["claude/sonnet"],
    attachment_max_file_bytes: 10485760,
    attachment_max_total_bytes_per_message: 26214400,
    attachment_max_files_per_message: 10,
    attachment_unbound_retention_hours: 24,
    attachment_gc_interval_minutes: 30,
  },
  voice: {
    enabled: true,
    tts_enabled: true,
    tts_provider: "chatterbox",
    tts_reference_audio: "~/.gobby/voice/reference.wav",
    tts_reference_text: null,
    tts_temperature: 0.8,
    tts_chatterbox_max_generation_tokens: 1000,
    tts_clause_max_chars: 220,
    tts_device: "auto",
    stt_enabled: true,
    transcription_timeout_seconds: 60,
    whisper_model_size: "base",
    whisper_device: "auto",
    whisper_compute_type: "int8",
    whisper_prompt: "Gobby",
    whisper_vocabulary: ["Gobby", "FalkorDB"],
    openai_compatible_audio: [
      {
        provider: "local-whisper",
        url: "http://localhost:8080/v1",
        model: "whisper-1",
        api_key: null,
        transcription_enabled: true,
        translation_enabled: false,
        timeout_seconds: 120,
      },
    ],
  },
  session_lifecycle: {
    active_session_pause_minutes: 30,
    stale_session_timeout_hours: 24,
    expire_check_interval_minutes: 5,
    transcript_processing_interval_minutes: 10,
    transcript_processing_batch_size: 50,
    transcript_archive_dir: "~/.gobby/session_transcripts",
  },
  session_summary: {
    profile: "feature_mid",
    candidates: ["claude/sonnet"],
    enabled: true,
    prompt: "Summarize the session.",
    summary_file_path: ".gobby/session_summaries",
  },
  compact_handoff: { enabled: true, refresh_timeout_seconds: 60 },
  chat_history: { max_message_chars: 4000, max_total_chars: 40000 },
  message_tracking: {
    enabled: true,
    poll_interval: 1.0,
    debounce_delay: 0.5,
    max_message_length: 10000,
    broadcast_enabled: true,
  },
  verification_defaults: {
    unit_tests: "uv run pytest tests/ -v",
    type_check: "uv run mypy src/",
    lint: "uv run ruff check src/",
    format: "uv run ruff format --check src/",
    build: null,
    doc_tests: null,
    integration: null,
    security: "bandit -r src/",
    code_review: "coderabbit review --ci",
    custom: { smoke: "make smoke" },
  },
  validation_detection: {
    enabled: true,
    builtin_matchers_enabled: true,
    disabled_builtin_matcher_ids: [],
    recognized_wrappers: ["uv", "npx"],
    wrapper_rules: [],
    custom_matchers: [],
  },
  recommend_tools: {
    profile: "feature_mid",
    enabled: true,
    candidates: ["claude/sonnet"],
    prompt_path: null,
    hybrid_rerank_prompt_path: null,
    llm_prompt_path: null,
  },
  tool_summarizer: {
    profile: "feature_low",
    enabled: true,
    candidates: ["claude/haiku"],
    prompt_path: null,
    system_prompt_path: null,
    server_description_prompt_path: null,
    server_description_system_prompt_path: null,
  },
  import_mcp_server: {
    profile: "feature_mid",
    enabled: false,
    candidates: [],
    prompt_path: null,
    github_fetch_prompt_path: null,
    search_fetch_prompt_path: null,
  },
  project_verification_synthesis: {
    profile: "feature_high",
    candidates: ["claude/opus"],
    confidence_threshold: 0.7,
  },
  merge_resolution: { profile: "feature_mid", candidates: ["claude/sonnet"] },
  skill_description: { profile: "feature_low", candidates: ["claude/haiku"] },
  ai: {
    generation: {
      timeout_seconds: 120,
      candidate_timeout_seconds: 60,
      cli_candidate_timeout_seconds: 90,
      endpoints: {
        "lmstudio-local": {
          protocol: "lmstudio",
          wire_api: "chat-completions",
          api_base: "http://localhost:1234",
          model: "qwen3-coder",
          api_key: null,
          tool_chat: true,
          vision_extract: false,
        },
      },
      profile_defaults: { feature_mid: ["claude/sonnet", "codex/gpt-5"] },
    },
  },
  context_window_overrides: { opus: 200000, sonnet: 200000 },
  "gobby-tasks": {
    enabled: true,
    show_result_on_create: false,
    file_extraction: {
      file_extensions: [".py", ".ts"],
      known_files: ["README.md"],
      path_prefixes: ["src/", "web/"],
    },
    expansion: {
      profile: "feature_mid",
      candidates: ["claude/sonnet"],
      enabled: true,
      prompt_path: null,
      system_prompt_path: null,
      default_strategy: "auto",
      timeout: 300,
      pattern_criteria: {
        patterns: { refactor: ["Tests pass", "No API break"] },
        detection_keywords: { refactor: ["refactor", "rename"] },
      },
    },
    validation: {
      profile: "feature_mid",
      candidates: ["claude/sonnet"],
      enabled: true,
      system_prompt: "Validate the task.",
      prompt_path: null,
      criteria_prompt_path: null,
      criteria_system_prompt: "Generate criteria.",
      max_iterations: 3,
      close_review_prompt_max_chars: 20000,
      escalation_enabled: false,
      escalation_notify: "none",
      escalation_webhook_url: null,
      auto_generate_on_create: true,
      auto_generate_on_expand: true,
    },
  },
  workflow: { enabled: true, timeout: 300, debug_echo_context: false },
  tmux: {
    enabled: true,
    command: "tmux",
    socket_name: "gobby",
    socket_path: null,
    config_file: null,
    session_prefix: "gobby-",
    history_limit: 50000,
    wsl_distribution: null,
    idle_check_enabled: true,
    idle_timeout_seconds: 300,
    idle_reprompt_delay_seconds: 30,
    max_reprompt_attempts: 3,
    reasoning_watchdog_interrupt_enabled: true,
    reasoning_watchdog_settle_seconds: 2.5,
    init_timeout_seconds: 60,
    init_activity_grace_seconds: 5.0,
    registration_timeout_seconds: 30.0,
    auto_enter_approval_prompts: true,
    auto_enter_agent_terminals: false,
    auto_enter_agent_interval_seconds: 5,
  },
  cron: {
    enabled: true,
    check_interval_seconds: 30,
    max_concurrent_jobs: 5,
    running_timeout_seconds: 3600,
    cleanup_after_days: 30,
    backoff_delays: [60, 300, 900],
  },
  system_loops: { automation: { enabled: true, interval_seconds: 60 } },
  pipelines: {
    prompt_step: { profile: "feature_mid", candidates: ["claude/sonnet"] },
    nesting_depth_limit: 3,
  },
  mcp_client_proxy: {
    enabled: true,
    connect_timeout: 30,
    proxy_timeout: 45,
    tool_timeout: 20,
    tool_timeouts: { "slow-tool": 90, search_tools: 15 },
    search_mode: "llm",
    min_similarity: 0.3,
    top_k: 10,
    refresh_on_server_add: true,
    refresh_timeout: 300,
  },
  skills: {
    inject_core_skills: true,
    core_skills_path: "install/shared/skills/",
    injection_format: "summary",
    hubs: {
      clawd: {
        type: "clawdhub",
        base_url: "https://hub.example",
        repo: null,
        branch: null,
        path: null,
        auth_key_name: null,
      },
      "gh-skills": {
        type: "github-collection",
        base_url: null,
        repo: "anthropics/skills",
        branch: "main",
        path: "skills/",
        auth_key_name: "github_token",
      },
    },
  },
  memory: {
    enabled: true,
    backend: "local",
    auto_crossref: true,
    crossref_threshold: 0.7,
    crossref_max_links: 5,
    access_debounce_seconds: 2,
    code_link_min_score: 0.5,
    temporal_decay_half_life_days: 30,
    min_recall_score: 0.2,
    graph_edge_weighting: true,
    materialize_cooccurrence: false,
    graph_edge_decay: true,
    edge_half_life_days: 14,
    recall_signal_logging: false,
    recall_signal_log_path: null,
    kg: { profile: "feature_low", candidates: ["claude/haiku"] },
    dream: {
      profile: "feature_mid",
      candidates: ["codex/gpt-5-mini"],
      enabled: false,
      schedule_cron: "0 2 * * *",
      prompt_path: "prompts/dream.md",
      max_tokens: 4000,
      max_runtime_seconds: 14400,
      work_unit_timeout_seconds: 1500,
      evidence_channel_timeout_seconds: 30,
      evidence_retry_attempts: 3,
      evidence_phase_timeout_seconds: 210,
      min_action_confidence: 0.6,
      min_delete_confidence: 0.8,
      include_global_memories: true,
      reconcile_after_apply: true,
      reconcile_after_revert: false,
      purge_review_after_days: 30,
      purge_delete_after_days: 7,
    },
  },
  memory_recall: {
    profile: "feature_high",
    candidates: ["claude/sonnet"],
    enabled: true,
    candidate_limit: 50,
    min_score: 0.35,
    selection_min_score: 0.65,
  },
  knowledge_graph_queue: { interval_minutes: 10, batch_size: 25 },
  memory_backup: { enabled: true, backup_path: ".gobby/memories.jsonl" },
  wiki: {
    enabled: true,
    roots: [
      { scope: "project", path: "docs/wiki" },
      { scope: "global", path: "~/.gobby/wiki" },
    ],
    debounce_interval: 2.0,
    poll_interval: 5.0,
    ignore_globs: ["outputs/**", "node_modules/**"],
    codewiki_on_commit: true,
    codewiki_nightly_enabled: true,
    codewiki_nightly_schedule_cron: "0 3 * * *",
    codewiki_nightly_timezone: null,
  },
  logging: {
    level: "info",
    format: "text",
    dir: "~/.gobby/logs",
    max_size_mb: 10,
    backup_count: 5,
    llm_max_size_mb: 50,
    llm_backup_count: 5,
    runtime_max_size_mb: 50,
    growth_warn_mb_per_interval: 100,
  },
  telemetry: {
    service_name: "gobby-daemon",
    traces_enabled: true,
    traces_to_console: false,
    trace_sample_rate: 1.0,
    trace_retention_days: 7,
    metrics_enabled: true,
    exporter: {
      otlp_endpoint: "http://localhost:4317",
      otlp_protocol: "grpc",
      otlp_headers: { Authorization: "Bearer token" },
      prometheus_enabled: true,
    },
    llm_tracing: {
      enabled: false,
      capture_content: false,
      providers: ["anthropic", "openai"],
    },
  },
  metrics: { list_limit: 10000 },
  communications: {
    enabled: true,
    webhook_base_url: "https://gobby.example/webhooks",
    channel_defaults: {
      rate_limit_per_minute: 30,
      burst: 5,
      retry_count: 3,
      poll_interval_seconds: 30,
      retention_days: 90,
    },
    auto_create_sessions: true,
  },
  hooks: { adapter_timeout: 105, provider_timeout: 120 },
  hook_extensions: {
    websocket: {
      enabled: true,
      broadcast_events: ["session-start", "post-tool-use"],
      include_payload: true,
    },
    webhooks: {
      enabled: true,
      endpoints: [
        {
          name: "ci-bridge",
          url: "https://ci.example/hook",
          events: ["post-tool-use"],
          headers: { "X-Token": "abc" },
          timeout: 12,
          retry_count: 2,
          retry_delay: 1.5,
          can_block: false,
          fail_closed: true,
          enabled: true,
        },
      ],
      default_timeout: 10,
      async_dispatch: true,
    },
  },
  code_index: {
    enabled: true,
    maintenance_interval_seconds: 300,
    maintenance_index_timeout_seconds: 600,
    nightly_repair_enabled: true,
    nightly_repair_cron: "0 2 * * *",
    nightly_repair_timezone: null,
    nightly_repair_timeout_seconds: 3600,
    nightly_repair_concurrency: 4,
    maintenance_log_file: "~/.gobby/logs/code-index-maintenance.log",
    missing_root_purge_observations: 3,
    embedding_enabled: true,
    graph_enabled: true,
    symbol_summary: {
      enabled: true,
      batch_size: 25,
      profile: "feature_low",
      candidates: ["claude/haiku"],
      max_concurrency: 4,
      max_tokens: 512,
    },
    sync_worker_interval_seconds: 60,
    sync_worker_batch_size: 100,
  },
  indexing: { respect_gitignore: true },
  bin_freshness: {
    enabled: true,
    initial_delay_seconds: 60,
    interval_seconds: 21600,
    jitter_seconds: 300,
    github_timeout_seconds: 10,
  },
  digest: {
    enabled: true,
    profile: "feature_mid",
    candidates: ["claude/sonnet"],
    timeout: 120,
  },
  web_chat_sandbox: {
    enabled: true,
    extra_read_paths: ["/tmp"],
    extra_write_paths: ["/tmp/out"],
  },
  agent_sandbox: {
    enabled: true,
    extra_read_paths: ["/tmp"],
    extra_write_paths: ["/tmp/out"],
  },
  clones_dir: "~/.gobby/clones",
  worktrees_dir: "~/.gobby/worktrees",
};

const SECRET_KEYS = [
  "auth.password",
  "embeddings.api_key",
  "databases.qdrant.api_key",
  "databases.falkordb.password",
];

/** Build a nested JSON-schema `properties` chain for one dotted path. */
function setSchemaPath(
  root: { type: string; properties: Record<string, unknown> },
  dotted: string,
  leaf: unknown,
): void {
  const segments = dotted.split(".");
  let node = root;
  for (const segment of segments.slice(0, -1)) {
    const existing = node.properties[segment] as
      { type: string; properties: Record<string, unknown> } | undefined;
    const child = existing ?? { type: "object", properties: {} };
    node.properties[segment] = child;
    node = child;
  }
  node.properties[segments.at(-1) as string] = leaf;
}

function buildConfigSchema(): Record<string, unknown> {
  const root = { type: "object", properties: {} as Record<string, unknown> };
  const profileLeaf = {
    type: "string",
    enum: ["feature_low", "feature_mid", "feature_high"],
  };
  for (const prefix of [
    "recommend_tools",
    "tool_summarizer",
    "import_mcp_server",
    "project_verification_synthesis",
    "merge_resolution",
    "skill_description",
    "chat",
    "session_summary",
    "gobby-tasks.expansion",
    "gobby-tasks.validation",
    "pipelines.prompt_step",
    "memory.kg",
    "memory.dream",
    "memory_recall",
    "code_index.symbol_summary",
    "digest",
  ]) {
    setSchemaPath(root, `${prefix}.profile`, profileLeaf);
  }
  setSchemaPath(root, "tool_approval.default_policy", {
    type: "string",
    enum: ["auto", "approve_once", "always_ask"],
  });
  setSchemaPath(root, "gobby-tasks.expansion.default_strategy", {
    type: "string",
    enum: ["auto", "phased", "sequential", "parallel"],
  });
  setSchemaPath(root, "gobby-tasks.validation.escalation_notify", {
    type: "string",
    enum: ["webhook", "slack", "none"],
  });
  setSchemaPath(root, "mcp_client_proxy.search_mode", {
    type: "string",
    enum: ["llm", "semantic", "hybrid"],
  });
  setSchemaPath(root, "skills.injection_format", {
    type: "string",
    enum: ["summary", "full", "none"],
  });
  setSchemaPath(root, "logging.level", {
    type: "string",
    enum: ["debug", "info", "warning", "error"],
  });
  setSchemaPath(root, "logging.format", {
    type: "string",
    enum: ["text", "json"],
  });
  setSchemaPath(root, "telemetry.exporter.otlp_protocol", {
    type: "string",
    enum: ["grpc", "http"],
  });
  setSchemaPath(root, "search.mode", {
    type: "string",
    enum: ["keyword", "embedding", "hybrid"],
  });
  return root;
}

const CONFIG_SCHEMA = buildConfigSchema();

// ---------------------------------------------------------------------------
// Mock plumbing
// ---------------------------------------------------------------------------

type ApiResolver = (
  path: string,
  url: URL,
  cell: CaptureCell,
) => unknown | "hang" | undefined;

/** Baseline API dataset — every surface renders seeded rows from this. */
function baseApi(
  pathname: string,
  url: URL,
  cell: CaptureCell,
  settings: Record<string, unknown>,
): unknown | undefined {
  switch (pathname) {
    case "/api/auth/status":
      return { authenticated: true };
    case "/api/config/ui-settings":
      // Must mirror the localStorage seed — the remote payload merges over
      // local settings on mount, so a mismatch would undo per-cell settings.
      return {
        selectedProjectId: PROJECT_ID,
        selectedProvider: "claude",
        ...settings,
        theme: cell.theme,
      };
    case "/api/projects":
    case "/api/files/projects":
      return [PROJECT];
    case "/api/providers":
      return {
        providers: [
          { name: "claude", available: true },
          { name: "codex", available: true },
        ],
      };
    case "/api/providers/models":
      return PROVIDER_CATALOG;
    case "/api/voice/status":
      return { enabled: false, stt_available: false };
    case "/api/sessions":
      return { sessions: SESSIONS, total: SESSIONS.length, next_cursor: null };
    case "/api/agents/running":
      return { agents: [] };
    case "/api/attention/roster":
      return { epoch: "capture-epoch", seq: 0, entries: [] };
    case "/api/skills/stats":
      return { total_count: 1, by_type: { impeccable: 1 }, recent_count: 0 };
    case "/api/stages/registry":
      return { stages: [PLANNING_STAGE, STAGE_ROW] };
    case "/api/profiles":
      return {
        profiles: [
          {
            id: "profile-1",
            name: "default",
            display_label: "Default",
            description: "Standard build profile",
            skip_stages: [],
            isolation: "worktree",
            unattended: false,
            delivery_mode: "auto",
            delivery_target_repo: null,
            enabled: true,
            source: "installed",
            project_id: null,
            tags: [],
            deleted_at: null,
            state: "bundled",
          },
        ],
      };
    case "/api/tasks": {
      if (url.searchParams.get("parent_task_id")) {
        return { tasks: [], total: 0, stats: {}, limit: 200, offset: 0 };
      }
      if (url.searchParams.get("closed") === "true") {
        return { tasks: [], total: 0, stats: {}, limit: 20, offset: 0 };
      }
      return { tasks: [TASK], total: 1, stats: {}, limit: 500, offset: 0 };
    }
    case `/api/tasks/${TASK.id}`:
      return { task: TASK };
    case `/api/tasks/${TASK.id}/dependencies`:
      return { id: TASK.id, blockers: [], blocking: [] };
    case "/api/mcp/servers":
      return {
        servers: [
          {
            name: "gobby",
            state: "connected",
            connected: true,
            available: true,
            transport: "stdio",
            enabled: true,
            description: "Gobby core tools",
            url: null,
            command: "gobby",
            args: [],
            project_id: null,
          },
          {
            name: "context7",
            state: "connected",
            connected: true,
            available: true,
            transport: "http",
            enabled: true,
            description: "Library docs",
            url: "https://mcp.context7.com/mcp",
            command: null,
            args: [],
            project_id: null,
          },
        ],
      };
    case "/api/mcp/tools":
      return {
        tools: {
          gobby: [
            {
              name: "list_tools",
              brief: "List available MCP tools",
              call_count: 12,
              success_rate: 1.0,
              avg_latency_ms: 42,
            },
            {
              name: "call_tool",
              brief: "Invoke a proxied MCP tool",
              call_count: 5,
              success_rate: 0.8,
              avg_latency_ms: 130,
            },
          ],
          context7: [
            {
              name: "resolve-library-id",
              brief: "Resolve a package to a docs id",
              call_count: 0,
              success_rate: null,
              avg_latency_ms: null,
            },
          ],
        },
      };
    case "/api/mcp/status":
      return {
        total_servers: 2,
        connected_servers: 2,
        cached_tools: 3,
        server_health: {
          gobby: { state: "connected", health: "healthy", failures: 0 },
          context7: { state: "connected", health: "healthy", failures: 0 },
        },
      };
    case "/api/agents/definitions":
      return { status: "success", definitions: [AGENT_DEFINITION] };
    case "/api/pipelines/definitions":
      return {
        status: "success",
        definitions: [PIPELINE_DEFINITION],
        count: 1,
      };
    case "/api/variables":
      return {
        status: "success",
        variables: [VARIABLE_DEFINITION],
        count: 1,
      };
    case "/api/skills":
      return { skills: [SKILL] };
    case `/api/skills/${SKILL.id}/files`:
      return {
        files: [
          {
            path: "SKILL.md",
            file_type: "markdown",
            size_bytes: 120,
            content_hash: "abc123",
          },
        ],
      };
    case `/api/skills/${SKILL.id}/files/SKILL.md`:
      return { content: SKILL.content };
    case "/api/rules":
      return {
        status: "success",
        rules: [RULE],
        count: 1,
        enforcement_enabled: true,
        aggregate_blocks: true,
      };
    case "/api/rules/groups":
      return { groups: ["safety"] };
    case "/api/rules/tags":
      return { tags: ["security"] };
    case `/api/rules/${RULE.name}`:
      return { rule: RULE };
    case "/api/memories":
      return { memories: [MEMORY] };
    case "/api/memories/stats":
      return {
        total_count: 1,
        by_type: { fact: 1 },
        recent_count: 1,
        avg_importance: 0.8,
        project_id: PROJECT_ID,
      };
    case "/api/memories/graph/entities":
      return {
        entities: [
          {
            entity_key: "entity-1",
            name: "Gobby",
            entity_type: "project",
            project_id: PROJECT_ID,
            properties: { role: "daemon" },
            memory_count: 3,
            memory_preview: "Gobby runs the activity panel.",
          },
          {
            entity_key: "entity-2",
            name: "Activity Panel",
            entity_type: "concept",
            project_id: PROJECT_ID,
            properties: {},
            memory_count: 1,
            memory_preview: null,
          },
        ],
        relationships: [
          {
            source_key: "entity-1",
            target_key: "entity-2",
            type: "CONTAINS",
            properties: {},
          },
        ],
      };
    case "/api/config/values":
      return { values: CONFIG_VALUES, secret_keys: SECRET_KEYS };
    case "/api/config/schema":
      return CONFIG_SCHEMA;
    case "/api/config/tool-approvals/global":
      return {
        rules: ["tool:Write", "mcp:gobby-tasks:*"],
        default_rules: ["tool:Read", "tool:Glob", "tool:Grep"],
        built_in_exemptions: ["tool:TodoWrite", "tool:ExitPlanMode"],
      };
    case "/api/config/secrets":
      return {
        secrets: [
          {
            id: "sec-1",
            name: "anthropic_key",
            category: "api",
            description: "Anthropic API key",
            created_at: "2026-01-01T00:00:00Z",
            updated_at: "2026-01-01T00:00:00Z",
          },
          {
            id: "sec-2",
            name: "qdrant_key",
            category: "database",
            description: null,
            created_at: "2026-01-01T00:00:00Z",
            updated_at: "2026-01-01T00:00:00Z",
          },
        ],
        categories: ["general", "api", "database"],
      };
    case "/api/config/prompts":
      return {
        prompts: [
          {
            path: "agents/summary",
            description: "Summarize an agent run",
            category: "agents",
            source: "bundled",
            has_override: false,
          },
          {
            path: "tasks/expand",
            description: "Expand a task into subtasks",
            category: "tasks",
            source: "overridden",
            has_override: true,
          },
        ],
        categories: { agents: 1, tasks: 1 },
        total: 2,
        limit: 500,
        offset: 0,
        count: 2,
      };
    case "/api/config/template":
      return { content: "daemon_port: 8742\nmemory:\n  enabled: true\n" };
    case "/api/comms/channels":
      return [
        {
          id: "chan-slack-1",
          channel_type: "slack",
          name: "Slack Alerts",
          enabled: true,
          config_json: { webhook_url: "https://hooks.slack.test/x" },
          webhook_secret: null,
          created_at: "2026-04-08T12:00:00Z",
          updated_at: "2026-04-08T12:00:00Z",
        },
        {
          id: "chan-tg-1",
          channel_type: "telegram",
          name: "Telegram Ops",
          enabled: false,
          config_json: { bot_token: "***", chat_id: "123" },
          webhook_secret: null,
          created_at: "2026-04-08T12:00:00Z",
          updated_at: "2026-04-08T12:00:00Z",
        },
      ];
    case "/api/wiki/pages":
      return {
        ok: true,
        command: "pages",
        payload: {
          pages: [
            {
              path: "Home.md",
              title: "Home",
              tags: ["seed"],
              content_hash: "abc123",
              updated_at: "2026-04-08T12:00:00Z",
            },
            {
              path: "knowledge/concepts/routing.md",
              title: "Routing",
              tags: [],
              content_hash: "def456",
              updated_at: "2026-04-08T12:00:00Z",
            },
          ],
          outputs: [],
        },
      };
    case "/api/wiki/status":
      return {
        ok: true,
        command: "status",
        payload: { status: "ready", services: { gwiki: { configured: true } } },
      };
    case "/api/wiki/health":
      return {
        ok: true,
        command: "health",
        payload: { broken_links: [], stale_pages: [], uncompiled_sources: [] },
      };
    case "/api/wiki/sources":
      return { ok: true, command: "sources", payload: { sources: [] } };
    case "/api/wiki/read":
      return {
        ok: true,
        command: "read",
        payload: {
          wiki_path: "Home.md",
          title: "Home",
          content: WIKI_LONG_CONTENT,
          content_hash: "abc123",
          status: "ok",
          truncated: false,
        },
      };
    case "/api/wiki/backlinks":
      return { ok: true, command: "backlinks", payload: { backlinks: [] } };
    case "/api/pipelines/executions":
      return {
        executions: [
          {
            id: "exec-1",
            pipeline_name: "nightly-verify",
            status: "completed",
            created_at: "2026-04-19T12:00:00Z",
            completed_at: "2026-04-19T12:04:00Z",
          },
          {
            id: "exec-2",
            pipeline_name: "nightly-verify",
            status: "failed",
            created_at: "2026-04-18T12:00:00Z",
            completed_at: "2026-04-18T12:02:00Z",
          },
        ],
      };
    case "/api/pipelines/exec-1":
      return {
        execution: {
          id: "exec-1",
          pipeline_name: "nightly-verify",
          status: "completed",
          created_at: "2026-04-19T12:00:00Z",
          completed_at: "2026-04-19T12:04:00Z",
          steps: [
            {
              step_id: "s1",
              name: "lint",
              status: "completed",
              started_at: "2026-04-19T12:00:10Z",
              completed_at: "2026-04-19T12:01:00Z",
            },
            {
              step_id: "s2",
              name: "tests",
              status: "completed",
              started_at: "2026-04-19T12:01:00Z",
              completed_at: "2026-04-19T12:04:00Z",
            },
          ],
        },
      };
    case "/api/cron/jobs":
      return {
        jobs: [
          {
            id: "cron-1",
            project_id: PROJECT_ID,
            name: "gobby.nightly-digest",
            display_name: "Nightly Digest",
            description: "Nightly summary",
            schedule_type: "cron",
            cron_expr: "0 3 * * *",
            interval_seconds: null,
            run_at: null,
            timezone: "UTC",
            action_type: "agent_spawn",
            action_config: {},
            enabled: true,
            is_system: false,
            next_run_at: "2026-04-27T03:00:00Z",
            last_run_at: "2026-04-19T03:00:00Z",
            last_status: "success",
            consecutive_failures: 0,
            created_at: "2026-04-08T12:00:00Z",
            updated_at: "2026-04-19T03:00:00Z",
          },
        ],
      };
    case "/api/cron/jobs/cron-1/runs":
      return {
        runs: [
          {
            id: "run-1",
            cron_job_id: "cron-1",
            triggered_at: "2026-04-19T03:00:00Z",
            started_at: "2026-04-19T03:00:01Z",
            completed_at: "2026-04-19T03:02:00Z",
            status: "success",
            output: null,
            error: null,
            agent_run_id: "agent-1",
            pipeline_execution_id: null,
            child: {
              type: "agent_run",
              id: "agent-1",
              status: "completed",
              terminal: true,
              missing: false,
            },
            created_at: "2026-04-19T03:00:00Z",
          },
        ],
      };
    case "/api/files/tree": {
      const treePath = url.searchParams.get("path") ?? "";
      if (treePath === "src") {
        return [
          {
            name: "main.py",
            path: "src/main.py",
            is_dir: false,
            extension: ".py",
            size: 1024,
          },
          {
            name: "utils.ts",
            path: "src/utils.ts",
            is_dir: false,
            extension: ".ts",
            size: 512,
          },
        ];
      }
      return [
        { name: "src", path: "src", is_dir: true, extension: null, size: null },
        {
          name: "README.md",
          path: "README.md",
          is_dir: false,
          extension: ".md",
          size: 256,
        },
      ];
    }
    case "/api/files/git-status":
      return {
        branch: "main",
        files: { "README.md": "M", "src/main.py": "??" },
      };
    case "/api/source-control/status":
      return {
        github_available: false,
        github_repo: null,
        current_branch: "main",
        branch_count: 1,
        worktree_count: 0,
        clone_count: 0,
      };
    case "/api/files/read":
      return {
        content: "# Readme\n\nSeeded file content for the capture harness.\n",
        image: false,
        binary: false,
        mime_type: "text/markdown",
        size: 52,
      };
    default:
      break;
  }
  // Session-scoped endpoints answer for ANY session id: the activity panel
  // adopts whichever session its list auto-selects, and the capture only
  // cares that rows render.
  if (/^\/api\/sessions\/[^/]+\/changes$/.test(pathname)) {
    return {
      files: [
        { path: "src/alpha.ts", status: "E" },
        { path: "src/beta.ts", status: "W" },
        { path: "src/gone.ts", status: "D" },
      ],
      isolation: "none",
    };
  }
  if (/^\/api\/sessions\/[^/]+\/changes\/diff$/.test(pathname)) {
    return { diff: "", path: url.searchParams.get("path") ?? "" };
  }
  if (/^\/api\/sessions\/[^/]+\/messages$/.test(pathname)) {
    return { messages: [], total: 0 };
  }
  if (/^\/api\/sessions\/[^/]+\/transcript\/status$/.test(pathname)) {
    return { status: "none" };
  }
  if (/^\/api\/sessions\/[^/]+$/.test(pathname)) {
    const id = pathname.split("/").at(-1);
    const row =
      id === DB_SESSION_ID
        ? CURRENT_SESSION
        : (SESSIONS.find((session) => session.id === id) ?? SESSIONS[0]);
    return { session: row };
  }
  if (/^\/api\/chat\/[^/]+\/messages$/.test(pathname)) {
    return { messages: CHAT_MESSAGES, max_seq: CHAT_MESSAGES.length };
  }
  if (pathname.startsWith("/api/config/prompts/")) {
    return {
      path: "agents/summary",
      description: "Summarize an agent run",
      content: "# Summary prompt\n\nSummarize the run.",
      source: "bundled",
      has_override: false,
      bundled_content: null,
      variables: { session_id: { type: "str", required: true, default: null } },
    };
  }
  if (pathname.startsWith("/api/comms/channels/")) {
    return {
      name: "Slack Alerts",
      channel_type: "slack",
      status: "connected",
      active: true,
      enabled: true,
      supports_webhooks: true,
      supports_polling: false,
      is_polling: false,
    };
  }
  return undefined;
}

type WsHook = (
  ws: WebSocketRoute,
  message: Record<string, unknown>,
  cell: CaptureCell,
) => void;

interface StateImpl {
  /** Extra localStorage entries seeded before app scripts run. */
  readonly localStorage?: (cell: CaptureCell) => Record<string, string>;
  /** Settings overrides merged into the `gobby-settings` seed. */
  readonly settings?: Record<string, unknown>;
  /** API overrides consulted before the base dataset. `"hang"` never
   * fulfills the request (loading states). */
  readonly api?: ApiResolver;
  /** WS hook invoked for every inbound client frame (after the base
   * subscribe handshake reply). */
  readonly ws?: WsHook;
  /** Post-goto interactions that reach the state. */
  readonly prepare?: (page: Page, cell: CaptureCell) => Promise<void>;
  /** The one visible checkpoint asserted before capture. */
  readonly checkpoint: (page: Page, cell: CaptureCell) => Locator;
  /** Extra readiness settling for asynchronous descendants. */
  readonly readiness?: (page: Page, cell: CaptureCell) => Promise<void>;
  /** The animated element a reduced-motion pair asserts computed styles on. */
  readonly motionTarget?: (page: Page) => Locator;
}

const DEFAULT_SETTINGS = {
  model: "opus",
  fontSize: 16,
  defaultChatMode: "plan",
  sttEnabled: false,
  ttsEnabled: false,
  voiceInputMode: "ptt",
  planPendingVariant: "info",
};

function tabSeed(tab: string, layout: "split" | "panel" | "chat" = "split") {
  return () => ({
    "gobby-activity-panel-tab-v2": tab,
    "gobby-activity-panel-layout": layout,
  });
}

/** On mobile-tier viewports the panel starts closed; the custom event opens
 * it and selects the tab on every tier. Dispatch races React's listener
 * registration, so retry until the panel content appears. */
async function showActivityTab(page: Page, tab: string): Promise<void> {
  await expect(page.getByTestId("app-header")).toBeVisible();
  await expect(async () => {
    await page.evaluate((tabId) => {
      window.dispatchEvent(
        new CustomEvent("gobby:show-activity-tab", { detail: { tab: tabId } }),
      );
    }, tab);
    await expect(page.locator(".activity-panel-content")).toBeVisible({
      timeout: 1000,
    });
  }).toPass({ timeout: 15000 });
}

function isMobileCell(cell: CaptureCell): boolean {
  // Mirror the CSS `mobile` custom variant (tailwind-theme.css): mobile is
  // max-width 767px OR max-height 500px, so the landscape viewport
  // (932×430) is mobile-tier and starts with the activity panel closed.
  return cell.viewport.width < 768 || cell.viewport.height <= 500;
}

function tabImpl(
  tab: string,
  checkpoint: (page: Page, cell: CaptureCell) => Locator,
  extra?: Partial<StateImpl>,
): StateImpl {
  return {
    localStorage: tabSeed(tab),
    prepare: async (page, cell) => {
      if (isMobileCell(cell)) {
        await showActivityTab(page, tab);
      }
      await extra?.prepare?.(page, cell);
    },
    checkpoint,
    ...(extra?.api ? { api: extra.api } : {}),
    ...(extra?.ws ? { ws: extra.ws } : {}),
    ...(extra?.readiness ? { readiness: extra.readiness } : {}),
  };
}

/** Reply hooks for the chat conversation socket. */
function chatReplyHook(
  reply: (ws: WebSocketRoute, requestId: string) => void,
): WsHook {
  return (ws, message) => {
    if (
      message.type === "chat_message" &&
      typeof message.request_id === "string"
    ) {
      reply(ws, message.request_id);
    }
  };
}

const TAB_CHECKPOINTS: Record<
  string,
  (page: Page, cell: CaptureCell) => Locator
> = {
  sessions: (page) => page.locator(".session-entry", { hasText: "Session A" }),
  terminal: (page) =>
    page
      .getByTestId("terminal-view")
      .locator(".term-row", { hasText: "plain line" })
      .first(),
  tasks: (page) =>
    page.getByTestId("task-tree").getByRole("treeitem", { name: /#14425/ }),
  mcp: (page) =>
    page
      .getByRole("tree", { name: "MCP servers and tools" })
      .getByRole("treeitem", { name: /gobby server/ }),
  agents: (page) => page.getByRole("button", { name: "Select reviewer" }),
  stages: (page) => page.getByRole("button", { name: "Select Development" }),
  skills: (page) => page.getByRole("button", { name: "Select impeccable" }),
  memory: (page) =>
    page.getByRole("list", { name: "Memories" }).getByRole("listitem").first(),
  integrations: (page) =>
    page.getByRole("button", { name: "Select Slack Alerts" }),
  wiki: (page) =>
    page
      .getByRole("tree", { name: "Wiki pages" })
      .getByRole("treeitem", { name: "Home" }),
  rules: (page) =>
    page.getByRole("button", { name: "Select no-secrets-in-diff" }),
  plans: (page) => page.getByTestId("plan-review-status"),
  changes: (page) => page.getByRole("button", { name: /alpha\.ts/ }),
  files: (page) =>
    page
      .getByRole("tree", { name: "Project files" })
      .getByRole("treeitem")
      .first(),
  pipelines: (page) =>
    page.getByRole("button", { name: /nightly-verify/ }).first(),
  cron: (page) => page.getByRole("button", { name: "Select Nightly Digest" }),
};

function buildTabImplementations(): Record<string, Record<string, StateImpl>> {
  const impls: Record<string, Record<string, StateImpl>> = {};
  for (const tab of ACTIVITY_PANEL_TABS) {
    const checkpoint = TAB_CHECKPOINTS[tab.id];
    if (!checkpoint) {
      throw new Error(
        `No checkpoint implementation for activity tab "${tab.id}"`,
      );
    }
    let base: StateImpl;
    switch (tab.id) {
      case "terminal": {
        const sentTerminalOutput = new WeakSet<WebSocketRoute>();
        base = {
          // Terminal renders in the bottom dock, never as panel content;
          // seeding the dock open key is the entire route to the state.
          localStorage: () => ({
            "gobby-terminal-dock-open": "true",
            "gobby-activity-panel-layout": "chat",
            "gobby:terminal:selected-target":
              '{"socket":"default","sessionName":"capture-session"}',
          }),
          ws: (ws, message) => {
            if (message.type === "tmux_list_sessions") {
              ws.send(
                JSON.stringify({
                  type: "tmux_sessions_list",
                  request_id: message.request_id ?? "init",
                  live_cli_session_ids: [],
                  sessions: [
                    {
                      name: "capture-session",
                      socket: "default",
                      pane_pid: 12345,
                      pane_dead: false,
                      pane_title: "Capture fixture",
                      pane_command: "zsh",
                      pane_path: "/tmp/project-one",
                      window_name: "capture",
                      session_title: "Capture fixture",
                      gobby_session_id: null,
                      agent_managed: false,
                      agent_run_id: null,
                      attached_bridge: null,
                    },
                  ],
                }),
              );
            }
            if (message.type === "tmux_attach") {
              ws.send(
                JSON.stringify({
                  type: "tmux_attach_result",
                  request_id: message.request_id,
                  success: true,
                  streaming_id: "stream-capture-session",
                }),
              );
            }
            if (message.type === "tmux_resize" && !sentTerminalOutput.has(ws)) {
              // Once per socket: repeated resize events must not duplicate
              // the seeded scrollback.
              sentTerminalOutput.add(ws);
              ws.send(
                JSON.stringify({
                  type: "terminal_output",
                  run_id: "stream-capture-session",
                  data: "[1;37m=== gobby capture ===[0m\r\nplain line\r\n$ ",
                }),
              );
            }
            if (message.type === "tmux_detach") {
              ws.send(
                JSON.stringify({
                  type: "tmux_detach_result",
                  request_id: message.request_id,
                  success: true,
                }),
              );
            }
          },
          checkpoint,
          readiness: async (page) => {
            // The renderer locks each wterm element's height to its fitted
            // grid, and the fit re-runs on font load and debounced container
            // resizes — a capture between fits shifts the whole dock by a
            // row. Require the seeded scrollback to be rendered and every
            // fitted box to hold still across consecutive checks.
            const wterms = page.locator(".wterm");
            await expect(wterms.first()).toBeVisible({ timeout: 20000 });
            await expect(page.getByText("plain line").first()).toBeVisible({
              timeout: 15000,
            });
            const boxes = () =>
              wterms.evaluateAll((els) =>
                els
                  .map((el) => {
                    const r = el.getBoundingClientRect();
                    return `${r.x},${r.y},${r.width},${r.height}`;
                  })
                  .join("|"),
              );
            await expect(async () => {
              const first = await boxes();
              await page.evaluate(
                () =>
                  new Promise<void>((resolve) => {
                    requestAnimationFrame(() =>
                      requestAnimationFrame(() => resolve()),
                    );
                  }),
              );
              const second = await boxes();
              if (!first || first !== second) {
                throw new Error("terminal still fitting");
              }
            }).toPass({ timeout: 15000, intervals: [300] });
          },
        };
        break;
      }
      case "plans":
        let planSocket: WebSocketRoute | undefined;
        let resolvePlanSocket: (socket: WebSocketRoute) => void;
        const planSocketReady = new Promise<WebSocketRoute>((resolve) => {
          resolvePlanSocket = resolve;
        });
        base = {
          localStorage: tabSeed("plans"),
          ws: (ws, message) => {
            const events = message.events;
            const isChatSocket =
              Array.isArray(events) && events.includes("chat_stream");
            if (message.type === "subscribe" && isChatSocket) {
              planSocket = ws;
              resolvePlanSocket(ws);
            }
          },
          prepare: async (page) => {
            // The app subscribes before every in-app consumer has registered
            // its handler, so a single send can land in a gap and vanish.
            // Re-drive the same event until the review banner mounts —
            // duplicate sends render the identical latest-plan banner, so
            // re-sending is pixel-neutral.
            const ws = planSocket ?? (await planSocketReady);
            const send = () =>
              ws.send(
                JSON.stringify({
                  type: "plan_pending_approval",
                  conversation_id: CONVERSATION_ID,
                  plan_content:
                    "# Implementation Plan\n\n1. Seed the panel\n2. Photograph every surface\n",
                  options: [{ id: "accept", label: "Approve" }],
                }),
              );
            send();
            await expect(async () => {
              if (await page.getByTestId("plan-review-status").isVisible()) {
                return;
              }
              send();
              throw new Error("plan review banner not mounted yet");
            }).toPass({ timeout: 20000, intervals: [500, 1000, 2000] });
          },
          checkpoint,
        };
        break;
      default:
        base = tabImpl(tab.id, checkpoint);
        break;
    }
    const states: Record<string, StateImpl> = { base };
    if (tab.id === "sessions") {
      states["filter-open"] = tabImpl(
        tab.id,
        (page) => page.getByTestId("sessions-filter-overlay"),
        {
          prepare: async (page) => {
            await page.getByRole("button", { name: "Filter sessions" }).click();
          },
        },
      );
      states["menu-open"] = tabImpl(
        tab.id,
        (page) =>
          page
            .locator(".activity-panel-mobile-menu")
            .getByRole("button", { name: "Tasks", exact: true }),
        {
          prepare: async (page) => {
            await page.locator(".activity-panel-mobile-trigger").click();
          },
        },
      );
    }
    if (tab.id === "wiki") {
      states.overflow = tabImpl(
        tab.id,
        (page) => page.getByRole("heading", { name: "Home" }).first(),
        {
          prepare: async (page) => {
            await page
              .getByRole("tree", { name: "Wiki pages" })
              .getByRole("treeitem", { name: "Home" })
              .click();
          },
        },
      );
    }
    impls[`tab-${tab.id}`] = states;
  }
  return impls;
}

function settingsImpl(sectionId: string, label: string): StateImpl {
  return {
    prepare: async (page, cell) => {
      page.on("dialog", (dialog) => void dialog.accept());
      await page.getByRole("button", { name: "Open settings" }).click();
      const dialog = page.getByRole("dialog", { name: "Settings" });
      await expect(dialog).toBeVisible();
      if (sectionId !== "appearance") {
        await dialog.locator('[aria-haspopup="listbox"]').click();
        // dispatchEvent: on short viewports the 13-option listbox clips
        // beyond the dialog's overflow:hidden bounds; this is a capture
        // harness, not an interaction test.
        await dialog
          .getByRole("option", { name: label, exact: true })
          .dispatchEvent("click");
      }
      void cell;
    },
    checkpoint: (page) =>
      page
        .getByRole("dialog", { name: "Settings" })
        .getByRole("heading", { level: 3, name: label }),
  };
}

function buildImplementations(): Record<string, Record<string, StateImpl>> {
  const impls: Record<string, Record<string, StateImpl>> = {
    login: {
      base: {
        api: (pathname) =>
          pathname === "/api/auth/status"
            ? { authenticated: false }
            : undefined,
        checkpoint: (page) => page.getByRole("button", { name: "Sign in" }),
      },
    },
    chat: {
      base: {
        checkpoint: (page) =>
          page.getByRole("heading", { name: "Styling consolidation" }),
      },
      overflow: {
        api: (pathname) =>
          /^\/api\/chat\/[^/]+\/messages$/.test(pathname)
            ? { messages: OVERFLOW_MESSAGES, max_seq: OVERFLOW_MESSAGES.length }
            : undefined,
        checkpoint: (page) =>
          page.getByText("Long-form content exercising mono overflow", {
            exact: false,
          }),
      },
      "stream-error": {
        ws: chatReplyHook((ws, requestId) => {
          ws.send(
            JSON.stringify({
              type: "chat_error",
              conversation_id: CONVERSATION_ID,
              request_id: requestId,
              error: "Provider connection lost mid-response.",
            }),
          );
        }),
        prepare: async (page) => {
          const input = page.getByRole("textbox", { name: /message input/i });
          await input.fill("Trigger an error response");
          await input.press("Enter");
        },
        checkpoint: (page) =>
          page.getByText("Provider connection lost mid-response.", {
            exact: false,
          }),
      },
      streaming: {
        // No server reply at all: sending a message sets the client's
        // thinking state, and the ThinkingIndicator shows while the last
        // message is the user's. Any `chat_thinking` frame would APPEND an
        // assistant message and hide the indicator.
        prepare: async (page) => {
          const input = page.getByRole("textbox", { name: /message input/i });
          await input.fill("Stream a response");
          await input.press("Enter");
        },
        checkpoint: (page) => page.getByText("Thinking...", { exact: false }),
        motionTarget: (page) => page.locator(".animate-spin").first(),
      },
      loading: {
        api: (pathname) =>
          /^\/api\/chat\/[^/]+\/messages$/.test(pathname) ? "hang" : undefined,
        checkpoint: (page) => page.getByText("Loading messages..."),
        motionTarget: (page) => page.locator("p.animate-pulse").first(),
      },
    },
    composer: {
      base: {
        prepare: async (page) => {
          const input = page.getByRole("textbox", { name: /message input/i });
          await input.fill("Draft a release note for 0.5.0");
        },
        checkpoint: (page) =>
          page.getByRole("button", { name: "Send message" }),
      },
      "voice-recording": {
        settings: { sttEnabled: true, voiceInputMode: "ptt" },
        api: (pathname) =>
          pathname === "/api/voice/status"
            ? {
                enabled: true,
                stt_enabled: true,
                stt_available: true,
                tts_enabled: false,
                tts_available: false,
                voice_ready: true,
                voice_loading: false,
              }
            : undefined,
        prepare: async (page) => {
          // Tap-to-latch: a quick press releases before the 250ms hold
          // threshold, so the recording latches on and persists through the
          // capture without holding a pointer down. click() also brings
          // Playwright's stability waits, unlike raw mouse coordinates.
          const mic = page.getByRole("button", { name: "Start push to talk" });
          await expect(mic).toBeVisible();
          await mic.click();
        },
        checkpoint: (page) =>
          page.getByRole("button", { name: "Push to talk recording" }),
        motionTarget: (page) =>
          page.locator(".chat-input-primary-button.animate-pulse"),
      },
      "voice-listening": {
        settings: { sttEnabled: true, voiceInputMode: "vad" },
        api: (pathname) =>
          pathname === "/api/voice/status"
            ? {
                enabled: true,
                stt_enabled: true,
                stt_available: true,
                tts_enabled: false,
                tts_available: false,
                voice_ready: true,
                voice_loading: false,
              }
            : undefined,
        checkpoint: (page) => page.getByTestId("voice-status-bar"),
        readiness: async (page) => {
          await expect(page.getByTestId("voice-status-bar")).toContainText(
            /Ready — speak to send|Listening/,
            { timeout: 15000 },
          );
        },
        motionTarget: (page) =>
          page
            .getByTestId("voice-status-bar")
            .locator(".animate-pulse, .animate-spin")
            .first(),
      },
    },
    ...buildTabImplementations(),
    "agents-editor": {
      base: {
        localStorage: tabSeed("agents"),
        prepare: async (page, cell) => {
          if (isMobileCell(cell)) {
            await showActivityTab(page, "agents");
          }
          await expect(
            page.getByRole("button", { name: "Select reviewer" }),
          ).toBeVisible();
          await page.getByRole("button", { name: "New agent" }).click();
        },
        checkpoint: (page) => page.getByRole("textbox", { name: "Name" }),
      },
    },
    "memory-graph": {
      base: {
        localStorage: tabSeed("memory"),
        prepare: async (page) => {
          // Desktop-tier viewports only (the matrix restricts this
          // scenario): the panel is already open and Show Graph exists.
          await expect(
            page
              .getByRole("list", { name: "Memories" })
              .getByRole("listitem")
              .first(),
          ).toBeVisible();
          await page.getByRole("button", { name: "Show Graph" }).click();
        },
        checkpoint: (page) => page.locator(".knowledge-graph"),
        readiness: async (page) => {
          await expect(
            page
              .locator(".knowledge-graph")
              .getByText("2 entities · 1 relationships"),
          ).toBeVisible({ timeout: 20000 });
          const canvas = page.locator(".knowledge-graph canvas");
          await expect(canvas).toBeVisible();
          // The WebGL scene animates forever (desktop link particles run on
          // a rAF loop that page.screenshot's animations:"disabled" cannot
          // freeze), so no amount of waiting yields a stable frame. Canvas
          // pixels are CSS-inert; hide it once the graph has provably
          // mounted so paired runs compare only the CSS-styled chrome.
          await canvas.evaluate((el) => {
            el.style.visibility = "hidden";
          });
        },
      },
    },
    "mobile-toolbar": {
      base: {
        prepare: async (page) => {
          await page
            .getByRole("button", { name: "Show activity panel" })
            .click();
        },
        checkpoint: (page) =>
          page
            .locator(".activity-panel-mobile-overlay")
            .getByRole("button", { name: "Close panel" }),
      },
    },
  };

  for (const section of SETTINGS_SECTIONS) {
    impls[`settings-${section.id}`] = {
      base: settingsImpl(section.id, section.label),
    };
  }

  return impls;
}

const IMPLEMENTATIONS = buildImplementations();

// ---------------------------------------------------------------------------
// Surfaces the matrix cannot photograph — recorded equivalence rationales.
// ---------------------------------------------------------------------------

interface RepresentativeMapping {
  readonly surface: string;
  readonly componentFile: string;
  readonly coveredBy: readonly string[];
  readonly rationale: string;
}

const REPRESENTATIVE_MAPPINGS: readonly RepresentativeMapping[] = [
  {
    surface: "TracesTab",
    componentFile: "components/activity/TracesTab.tsx",
    coveredBy: ["components/activity/__tests__/TracesTab.test.tsx"],
    rationale:
      "Traces is deliberately hidden from the tab strip (moat 66e919e3, " +
      "#19152), so no live route reaches it; the 4.8 migration is covered " +
      "by its component tests.",
  },
  {
    surface: "CodeGraphExplorer",
    componentFile: "components/code-graph/CodeGraphExplorer.tsx",
    coveredBy: [
      "components/code-graph/__tests__/CodeGraphExplorer.test.tsx",
      "capture cell family: memory-graph (KnowledgeGraph as the graph-chrome representative)",
    ],
    rationale:
      "Zero production mounts (test-only imports); the 4.5 sweep is covered " +
      "by component tests plus the KnowledgeGraph capture, which exercises " +
      "the same graph chrome.",
  },
];

// ---------------------------------------------------------------------------
// Always-on roster and mapping guards (run in the default project)
// ---------------------------------------------------------------------------

test.describe("surface checkpoint assertion", () => {
  test("the scenario roster derives from the live registries", () => {
    const scenarios = buildCaptureScenarios();
    const ids = scenarios.map((scenario) => scenario.id);

    // A registry change fails loudly: one scenario per live tab, one
    // settings cell per live section.
    expect(ids.filter((id) => id.startsWith("tab-")).length).toBe(
      ACTIVITY_PANEL_TABS.length,
    );
    expect(ids.filter((id) => id.startsWith("settings-")).length).toBe(
      SETTINGS_SECTIONS.length,
    );
    for (const tab of ACTIVITY_PANEL_TABS) {
      expect(ids).toContain(`tab-${tab.id}`);
    }
    for (const section of SETTINGS_SECTIONS) {
      expect(ids).toContain(`settings-${section.id}`);
    }
  });

  test("every roster cell has exactly one implementation", () => {
    const scenarios = buildCaptureScenarios();
    const expected = new Set(
      scenarios.flatMap((scenario) =>
        scenario.states.map((state) => `${scenario.id}::${state.id}`),
      ),
    );
    const implemented = new Set(
      Object.entries(IMPLEMENTATIONS).flatMap(([scenario, states]) =>
        Object.keys(states).map((state) => `${scenario}::${state}`),
      ),
    );
    expect([...implemented].sort()).toEqual([...expected].sort());

    // Reduced-motion pairs must declare the element their computed-style
    // assertions target.
    for (const scenario of scenarios) {
      for (const state of scenario.states) {
        if (state.motionPair) {
          expect(
            IMPLEMENTATIONS[scenario.id]?.[state.id]?.motionTarget,
            `motionTarget missing for ${scenario.id}::${state.id}`,
          ).toBeTruthy();
        }
      }
    }
  });
});

test.describe("representative mappings", () => {
  test("unphotographable surfaces stay mapped to live coverage", () => {
    for (const mapping of REPRESENTATIVE_MAPPINGS) {
      expect(
        fs.existsSync(path.join(SRC_DIR, mapping.componentFile)),
        `${mapping.surface}: component ${mapping.componentFile} is gone — ` +
          `retire the mapping or photograph the surface.`,
      ).toBe(true);
      for (const covered of mapping.coveredBy) {
        if (covered.startsWith("capture cell family: ")) {
          continue;
        }
        expect(
          fs.existsSync(path.join(SRC_DIR, covered)),
          `${mapping.surface}: covering test ${covered} is gone.`,
        ).toBe(true);
      }
      expect(mapping.rationale.length).toBeGreaterThan(40);
    }

    // Traces must still be absent from the live registry — if it returns,
    // it needs a real capture scenario instead of a mapping.
    expect(
      ACTIVITY_PANEL_TABS.some((tab) => (tab.id as string) === "traces"),
    ).toBe(false);
  });
});

test.describe("composer parity", () => {
  test("the capture matrix covers the composer and both live voice states", () => {
    const composerCells = expandCaptureCells().filter(
      (cell) => cell.scenario === "composer",
    );

    expect(new Set(composerCells.map((cell) => cell.state))).toEqual(
      new Set(["base", "voice-recording", "voice-listening"]),
    );
    for (const sheet of [
      "input-base.css",
      "input-composer.css",
      "input-voice.css",
      "input-responsive.css",
      "input-status.css",
      "input.css",
    ]) {
      expect(
        fs.existsSync(path.join(SRC_DIR, "components/chat/styles", sheet)),
      ).toBe(false);
    }
  });
});

test.describe("reduced-motion relocation", () => {
  test("recording, listening, loading, and streaming pair both motion preferences", () => {
    const motionFamilies = [
      ["composer", "voice-recording"],
      ["composer", "voice-listening"],
      ["chat", "loading"],
      ["chat", "streaming"],
    ] as const;
    const cells = expandCaptureCells();

    for (const [scenario, state] of motionFamilies) {
      expect(
        new Set(
          cells
            .filter(
              (cell) => cell.scenario === scenario && cell.state === state,
            )
            .map((cell) => cell.motion),
        ),
      ).toEqual(new Set(["reduce", "none"]));
    }

    const accessibility = fs.readFileSync(
      path.join(SRC_DIR, "styles/accessibility.css"),
      "utf8",
    );
    expect(accessibility).toContain("prefers-reduced-motion: reduce");
    expect(accessibility).toContain(
      '.chat-input-primary-button[aria-pressed="true"]',
    );
    expect(accessibility).toContain(
      '.chat-input-voice-toggle[aria-busy="true"]',
    );
    expect(accessibility).toContain(".voice-status-bar [data-voice-motion]");
  });
});

test.describe("matrix parity review", () => {
  // The executable review gate for cascade-neutral flips (plan section 6.1):
  // two finalized labeled runs must agree byte-for-byte on every cell of the
  // CURRENT expected matrix. Opt-in — point both env vars at finalized run
  // labels under tests/screenshots/style-captures/runs/:
  //
  //   GOBBY_CAPTURE_PARITY_BEFORE=<sha>-before \
  //   GOBBY_CAPTURE_PARITY_AFTER=<sha>-after \
  //     npx playwright test tests/style-surfaces.spec.ts \
  //     --grep "matrix parity review" --project=chromium
  const BEFORE_ENV = "GOBBY_CAPTURE_PARITY_BEFORE";
  const AFTER_ENV = "GOBBY_CAPTURE_PARITY_AFTER";

  test("every matrix cell is pixel-identical across the labeled runs", () => {
    const beforeLabel = process.env[BEFORE_ENV];
    const afterLabel = process.env[AFTER_ENV];
    test.skip(
      !beforeLabel || !afterLabel,
      `opt-in: set ${BEFORE_ENV} and ${AFTER_ENV} to finalized capture-run labels`,
    );

    const loadManifest = (label: string): FinalizedRunManifest => {
      const manifestPath = path.join(
        runDirFor(captureRootDir(), label),
        "run-manifest.json",
      );
      expect(
        fs.existsSync(manifestPath),
        `no finalized capture run "${label}" at ${manifestPath}`,
      ).toBe(true);
      return JSON.parse(
        fs.readFileSync(manifestPath, "utf8"),
      ) as FinalizedRunManifest;
    };
    const before = loadManifest(beforeLabel!);
    const after = loadManifest(afterLabel!);

    // Both runs must cover the current expected matrix exactly — a stale or
    // shrunken run cannot vouch for parity.
    const expectedKeys = expandCaptureCells()
      .map((cell) => cell.key)
      .sort();
    expect(Object.keys(before.cells).sort()).toEqual(expectedKeys);
    expect(Object.keys(after.cells).sort()).toEqual(expectedKeys);

    const differing = expectedKeys.filter(
      (key) => before.cells[key]?.pngSha256 !== after.cells[key]?.pngSha256,
    );
    expect(
      differing,
      `${differing.length} cell(s) differ between "${beforeLabel}" and ` +
        `"${afterLabel}" — review each pair and fix every regression at its ` +
        `source (component specificity, never a new !important).`,
    ).toEqual([]);
  });
});

test.describe("settings overlay parity", () => {
  const BEFORE_ENV = "GOBBY_CAPTURE_PARITY_BEFORE";
  const AFTER_ENV = "GOBBY_CAPTURE_PARITY_AFTER";

  test("every settings cell is pixel-identical across the labeled runs", () => {
    const beforeLabel = process.env[BEFORE_ENV];
    const afterLabel = process.env[AFTER_ENV];
    test.skip(
      !beforeLabel || !afterLabel,
      `opt-in: set ${BEFORE_ENV} and ${AFTER_ENV} to finalized capture-run labels`,
    );

    const loadManifest = (label: string): FinalizedRunManifest => {
      const manifestPath = path.join(
        runDirFor(captureRootDir(), label),
        "run-manifest.json",
      );
      expect(
        fs.existsSync(manifestPath),
        `no finalized capture run "${label}" at ${manifestPath}`,
      ).toBe(true);
      return JSON.parse(
        fs.readFileSync(manifestPath, "utf8"),
      ) as FinalizedRunManifest;
    };
    const before = loadManifest(beforeLabel!);
    const after = loadManifest(afterLabel!);
    const settingsKeys = expandCaptureCells()
      .filter((cell) => cell.scenario.startsWith("settings-"))
      .map((cell) => cell.key)
      .sort();

    expect(settingsKeys.length).toBeGreaterThan(0);
    for (const key of settingsKeys) {
      expect(before.cells[key], `before run is missing ${key}`).toBeDefined();
      expect(after.cells[key], `after run is missing ${key}`).toBeDefined();
    }

    const differing = settingsKeys.filter(
      (key) => before.cells[key]?.pngSha256 !== after.cells[key]?.pngSha256,
    );
    expect(
      differing,
      `${differing.length} settings cell(s) differ between "${beforeLabel}" ` +
        `and "${afterLabel}" — fix each regression at its component utility.`,
    ).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// The capture matrix (opt-in, @style-capture project only)
// ---------------------------------------------------------------------------

async function settle(page: Page): Promise<void> {
  await page.evaluate(async () => {
    await document.fonts.ready;
    // Raster images paint progressively: a screenshot can catch a half
    // decoded logo. Wait for every <img> and every CSS background-image
    // URL to be fully loaded before capture.
    await Promise.all(
      Array.from(document.images, (img) => img.decode().catch(() => {})),
    );
    const bgUrls = new Set<string>();
    for (const el of Array.from(document.querySelectorAll("*"))) {
      const match =
        getComputedStyle(el).backgroundImage.match(/url\("?([^")]+)"?\)/);
      if (match) bgUrls.add(match[1]);
    }
    await Promise.all(
      Array.from(
        bgUrls,
        (url) =>
          new Promise<void>((resolve) => {
            const img = new Image();
            img.onload = () => resolve();
            img.onerror = () => resolve();
            img.src = url;
          }),
      ),
    );
    await new Promise((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(resolve)),
    );
  });
}

async function assertPointerEmulation(
  page: Page,
  cell: CaptureCell,
  opts?: { bestEffort?: boolean },
): Promise<void> {
  // Poll: touch-derived pointer emulation can apply a beat after load. On
  // the rendered document a page where it never applies fails loudly — that
  // is the contract. The pre-goto warm-up call is best-effort instead:
  // about:blank sometimes never re-evaluates its media queries after the
  // context-level touch emulation lands, while the navigation right after
  // commits a fresh document that picks the emulation up at commit — so a
  // warm-up timeout is noise, never evidence about the real page.
  try {
    await expect
      .poll(
        () =>
          page.evaluate(() => window.matchMedia("(pointer: coarse)").matches),
        {
          // Generous: emulation is context-level and always lands eventually,
          // but heavy first renders (agents editor, force graph) can starve
          // the poll well past 5s under parallel-worker load.
          timeout: opts?.bestEffort ? 5000 : 15000,
          message:
            `matchMedia pointer axis mis-emulated for ${cell.key}: expected ` +
            `pointer:${cell.pointer}`,
        },
      )
      .toBe(cell.pointer === "coarse");
  } catch (error) {
    if (!opts?.bestEffort) throw error;
  }
}

interface ComputedAnimation {
  name: string;
  durationSeconds: number;
}

async function computedAnimation(target: Locator): Promise<ComputedAnimation> {
  return target.evaluate((el) => {
    const style = getComputedStyle(el);
    return {
      name: style.animationName,
      durationSeconds: Number.parseFloat(style.animationDuration) || 0,
    };
  });
}

async function runCaptureCell(
  browser: Browser,
  cell: CaptureCell,
  testInfo: TestInfo,
): Promise<CaptureCellFragment> {
  const impl = IMPLEMENTATIONS[cell.scenario]?.[cell.state];
  if (!impl) {
    throw new Error(`No implementation for capture cell ${cell.key}`);
  }

  const context = await browser.newContext({
    baseURL: BASE_URL,
    viewport: { width: cell.viewport.width, height: cell.viewport.height },
    deviceScaleFactor: 1,
    hasTouch: cell.pointer === "coarse",
    colorScheme: cell.theme,
    reducedMotion: cell.motion === "reduce" ? "reduce" : "no-preference",
    // The voice cells exercise live mic capture against the fake device.
    permissions: ["microphone"],
  });
  const consoleLog: string[] = [];
  try {
    const page = await context.newPage();
    page.on("console", (message) => {
      if (message.type() === "error" || message.type() === "warning") {
        consoleLog.push(
          `console.${message.type()}: ${message.text().slice(0, 300)}`,
        );
      }
    });
    page.on("pageerror", (error) => {
      consoleLog.push(`pageerror: ${String(error).slice(0, 300)}`);
    });

    if (cell.pointer === "coarse") {
      // Belt over the context-level hasTouch: rarely a page target misses
      // the context's touch emulation entirely — pointer:coarse never
      // becomes true, even on the committed document. Re-issuing the same
      // CDP override on the page's own session pins it. The session stays
      // attached: Emulation overrides reset when their session detaches.
      const cdp = await context.newCDPSession(page);
      await cdp.send("Emulation.setTouchEmulationEnabled", {
        enabled: true,
        maxTouchPoints: 1,
      });
    }

    // Warm up the pointer axis on the blank page, before the app loads, so
    // the app almost always mounts with the emulated axis already in force.
    // Best-effort by design: see assertPointerEmulation — the hard contract
    // is the post-render assert on the real document.
    await assertPointerEmulation(page, cell, { bestEffort: true });

    // Deterministic clock: Date is FROZEN at the fixed epoch (constructor
    // and Date.now), while timers and performance.now keep running — only
    // page.clock-style full virtual time stalls scheduling and prevents
    // surfaces from mounting. Pinning Date alone keeps every client-side
    // stamp and relative label byte-identical across paired runs.
    // Lazy mounts (CodeBlock and any other IntersectionObserver consumer)
    // are bistable in captures: whether the observer fires before an
    // overlay/tab covers the target depends on load timing, so paired runs
    // can disagree on placeholder-vs-mounted pixels. Make every observer
    // fire immediately — captures always photograph the mounted state.
    await page.addInitScript(() => {
      class EagerIntersectionObserver {
        private readonly callback: IntersectionObserverCallback;
        readonly root = null;
        readonly rootMargin = "0px";
        readonly thresholds = [0];
        constructor(callback: IntersectionObserverCallback) {
          this.callback = callback;
        }
        observe(target: Element): void {
          this.callback(
            [
              {
                isIntersecting: true,
                target,
                intersectionRatio: 1,
                time: 0,
                boundingClientRect: target.getBoundingClientRect(),
                intersectionRect: target.getBoundingClientRect(),
                rootBounds: null,
              } as IntersectionObserverEntry,
            ],
            this as unknown as IntersectionObserver,
          );
        }
        unobserve(): void {}
        disconnect(): void {}
        takeRecords(): IntersectionObserverEntry[] {
          return [];
        }
      }
      window.IntersectionObserver =
        EagerIntersectionObserver as unknown as typeof IntersectionObserver;
    });

    // Fully frozen, never merely shifted: the clock must not advance during
    // the run, or client-side stamps (message receive times) tick between
    // paired runs — a rendered 7:00:00 vs 7:00:01 was a real diff class.
    await page.addInitScript((fixedNowMs: number) => {
      const OriginalDate = Date;
      class FrozenDate extends OriginalDate {
        constructor(...args: unknown[]) {
          if (args.length === 0) {
            super(fixedNowMs);
          } else {
            // @ts-expect-error variadic Date construction passthrough
            super(...args);
          }
        }
        static now(): number {
          return fixedNowMs;
        }
      }
      FrozenDate.parse = OriginalDate.parse;
      FrozenDate.UTC = OriginalDate.UTC;
      (window as { Date: unknown }).Date = FrozenDate;
    }, FIXED_TIME.getTime());

    const settings = {
      ...DEFAULT_SETTINGS,
      theme: cell.theme,
      ...(impl.settings ?? {}),
    };
    const storage: Record<string, string> = {
      "gobby-settings": JSON.stringify(settings),
      "gobby-conversation-id": CONVERSATION_ID,
      "gobby-db-session-id": DB_SESSION_ID,
      ...(impl.localStorage?.(cell) ?? {}),
    };
    await page.addInitScript((entries: Record<string, string>) => {
      for (const [key, value] of Object.entries(entries)) {
        localStorage.setItem(key, value);
      }
    }, storage);

    await page.routeWebSocket("**/ws", (ws) => {
      ws.onMessage((raw) => {
        let message: Record<string, unknown>;
        try {
          message = JSON.parse(String(raw)) as Record<string, unknown>;
        } catch {
          return;
        }
        if (message.type === "subscribe") {
          // Both ids: the client reconciles its persisted DB session id
          // against this list, and the changes tab scopes to it.
          ws.send(
            JSON.stringify({
              type: "connection_established",
              conversation_ids: [CONVERSATION_ID, DB_SESSION_ID],
            }),
          );
          ws.send(
            JSON.stringify({
              type: "subscribe_success",
              events: (message.events as string[] | undefined) ?? [],
            }),
          );
        }
        impl.ws?.(ws, message, cell);
      });
    });

    await page.route("**/api/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (request.method() !== "GET") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ ok: true }),
        });
        return;
      }
      const override = impl.api?.(url.pathname, url, cell);
      if (override === "hang") {
        // Deliberately never fulfilled: loading states.
        return;
      }
      const payload = override ?? baseApi(url.pathname, url, cell, settings);
      if (payload === undefined) {
        // Unmodeled endpoint — surfaced in the failure diagnostics so a
        // missing stub names itself instead of rendering an empty surface.
        consoleLog.push(`api-miss: ${url.pathname}${url.search}`);
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(payload ?? {}),
      });
    });

    await page.goto("/");
    await impl.prepare?.(page, cell);

    // The visible checkpoint: the run fails when it is absent.
    await expect(impl.checkpoint(page, cell)).toBeVisible({ timeout: 20000 });

    await assertPointerEmulation(page, cell);
    await settle(page);
    await impl.readiness?.(page, cell);

    // The activity panel's session list streams in after first paint; a
    // capture taken during its loading placeholder is nondeterministic
    // between paired runs. No cell hangs the sessions endpoint, so this gate
    // is unconditional — the loading cells only hang the MESSAGES fetch, and
    // their sessions panel still races to loaded run-to-run.
    await expect(page.getByText(/Loading sessions/)).toHaveCount(0, {
      timeout: 15000,
    });
    // Same race for the chat column's message fetch ("Loading messages…"
    // vs "No messages yet" flips run-to-run). Deliberate loading states
    // photograph that placeholder, so only they opt out.
    if (!cell.state.startsWith("loading")) {
      await expect(page.getByText(/Loading messages/)).toHaveCount(0, {
        timeout: 15000,
      });
    }

    if (cell.motion !== null) {
      const target = impl.motionTarget?.(page);
      if (!target) {
        throw new Error(`Motion cell ${cell.key} has no motionTarget`);
      }
      // Precondition: the emulated preference must actually be in force,
      // so a suppression failure is a CSS bug, never a mis-emulation.
      const rmMatches = await page.evaluate(
        () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
      );
      expect(
        rmMatches,
        `${cell.key}: prefers-reduced-motion emulation mismatch`,
      ).toBe(cell.motion === "reduce");
      const animation = await computedAnimation(target);
      if (cell.motion === "reduce") {
        // The global reduced-motion kill switch clamps durations to 0.01ms;
        // Tailwind motion-reduce variants may remove the animation outright.
        expect(
          animation.name === "none" || animation.durationSeconds <= 0.02,
          `${cell.key}: animation not suppressed under reduced motion ` +
            `(${animation.name} ${animation.durationSeconds}s)`,
        ).toBe(true);
      } else {
        expect(
          animation.name,
          `${cell.key}: control animation missing`,
        ).not.toBe("none");
        expect(animation.durationSeconds).toBeGreaterThan(0.1);
      }
    }

    // Pixel determinism: a screenshot must not sample a timing-dependent
    // animation frame, a mid-flight transition (hover/focus fades from
    // prepare interactions), or a fading macOS overlay scrollbar left by a
    // programmatic scroll. The motion contract was already asserted on
    // computed styles above, so freezing here loses nothing.
    // The freeze must sit INSIDE @layer utilities: for !important
    // declarations layer precedence is reversed (layered beats un-layered),
    // so with important:true active an un-layered freeze would lose to the
    // animate-*/transition-* utilities. Same layer + injected last wins in
    // both flag states.
    await page.addStyleTag({
      content:
        "@layer utilities { *, *::before, *::after { animation: none !important; transition: none !important; } " +
        "::-webkit-scrollbar { display: none !important; } }",
    });

    // Overlay scrollbars fade on their own compositor timeline after any
    // programmatic scroll; a mid-fade thumb leaves ±1-channel residue along
    // scroll-container edges that breaks paired-run byte equality. Remove
    // the scrollbar lane entirely for the shot — overlay bars occupy no
    // layout space, so geometry is unchanged.
    await page.addStyleTag({
      content: "* { scrollbar-width: none !important; }",
    });

    if (cell.grayscale) {
      // The repeatable deutan check: capture the state fully desaturated.
      await page.addStyleTag({
        content: "html { filter: grayscale(1) !important; }",
      });
    }

    // Always "disabled": the motion contract is enforced by the computed-style
    // assertions above, so the pixels themselves must be phase-deterministic —
    // Playwright rewinds infinite animations to their initial state and
    // fast-forwards finite ones identically in both runs of a parity pair.
    const png = await page.screenshot({
      caret: "hide",
      animations: "disabled",
    });
    return stageCaptureCell(testInfo, cell, png);
  } catch (error) {
    if (consoleLog.length > 0) {
      throw new Error(
        `${String(error)}\nBrowser console (errors/warnings):\n  ${consoleLog
          .slice(-15)
          .join("\n  ")}`,
        { cause: error },
      );
    }
    throw error;
  } finally {
    await context.close();
  }
}

test.describe("style-surface capture matrix", () => {
  test.describe.configure({ timeout: 90_000 });
  // The configure() timeout above proved inert on this runner — every
  // capture attempt in a full matrix run died at the 30s default — so pin
  // the per-test timeout imperatively as well.
  test.beforeEach(() => {
    test.setTimeout(90_000);
  });
  test.skip(
    !RUN_ID,
    `Set ${CAPTURE_RUN_ENV}=<label> (see the header comment) to run the capture matrix.`,
  );

  for (const cell of expandCaptureCells()) {
    test(`capture ${cell.key} @style-capture`, async ({
      browser,
    }, testInfo) => {
      const fragment = await runCaptureCell(browser, cell, testInfo);
      expect(fragment.cellKey).toBe(cell.key);
      expect(fragment.pngSha256).toMatch(/^[0-9a-f]{64}$/);
    });
  }
});
