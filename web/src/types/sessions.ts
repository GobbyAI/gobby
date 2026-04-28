export interface GobbySession {
  id: string;
  ref: string;
  external_id: string;
  source: string;
  project_id: string;
  title: string | null;
  title_source?: string | null;
  status: string;
  model: string | null;
  message_count: number;
  created_at: string;
  updated_at: string;
  seq_num: number | null;
  transcript_path?: string | null;
  summary_path?: string | null;
  summary_markdown: string | null;
  digest_markdown: string | null;
  git_branch: string | null;
  usage_input_tokens: number;
  usage_output_tokens: number;
  usage_cache_creation_tokens?: number;
  usage_cache_read_tokens?: number;
  context_window?: number | null;
  had_edits: boolean;
  agent_depth: number;
  chat_mode: string | null;
  agent_run_id: string | null;
  is_local?: boolean | null;
  workflow_name?: string | null;
  parent_session_id: string | null;
  session_type: string;
  terminal_context: Record<string, unknown> | null;
  sandbox_enabled?: boolean;
  sandbox_policy_hash?: string | null;
  can_proxy_attach?: boolean;
  tasks_closed?: number;
  memories_created?: number;
  commit_count?: number;
}

export const KNOWN_SOURCES = ["claude", "gemini", "qwen", "codex", "droid"] as const;

export interface SessionFilters {
  source: string | null;
  projectId: string | null;
  search: string;
  sortOrder: "newest" | "oldest";
}

export interface ProjectInfo {
  id: string;
  name: string;
  repo_path: string;
}
