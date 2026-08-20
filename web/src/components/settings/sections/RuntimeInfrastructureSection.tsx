import {
  NumberConfigField,
  OptionsSelectField,
  SchemaSelectField,
  StringListConfigField,
  Subsection,
  SwitchConfigField,
  TextConfigField,
} from "./configFields";
import { SettingsSection, type SettingsSectionFields } from "./SettingsSection";

/**
 * Config rows this section owns, per the configuration audit (IA section 13,
 * "Runtime & Infrastructure"). The draft is scoped to exactly these dotted
 * paths; every keep-row appears once. The bootstrap-owned rows (`hub_backend`,
 * `database_url`, `postgres_install_mode`) and the auto-generated
 * `auth.session_secret` are intentionally absent — they are dropped from the
 * overlay. Rows the legacy form rendered as a free-text fallback get real
 * editors here: `cors_origins`, the sandbox path lists, `digest.candidates`,
 * and code-index summary candidates become string lists, while `search.mode`
 * and `ui.mode` become bounded selects.
 */
const OWNED_PATHS: readonly string[] = [
  // Daemon core
  "daemon_port",
  "bind_host",
  "daemon_health_check_interval",
  "test_mode",
  "cors_origins",
  // WebSocket server
  "websocket.enabled",
  "websocket.port",
  "websocket.ping_interval",
  "websocket.ping_timeout",
  // Web UI serving
  "ui.enabled",
  "ui.mode",
  "ui.port",
  "ui.host",
  "ui.web_dir",
  "ui.knowledge_graph_limit",
  "ui.knowledge_graph_relationship_limit",
  // Unified search
  "search.mode",
  "search.keyword_weight",
  "search.embedding_weight",
  "search.notify_on_fallback",
  // Code index
  "code_index.enabled",
  "code_index.maintenance_interval_seconds",
  "code_index.maintenance_index_timeout_seconds",
  "code_index.nightly_repair_enabled",
  "code_index.nightly_repair_cron",
  "code_index.nightly_repair_timezone",
  "code_index.nightly_repair_timeout_seconds",
  "code_index.nightly_repair_concurrency",
  "code_index.maintenance_log_file",
  "code_index.missing_root_purge_observations",
  "code_index.embedding_enabled",
  "code_index.graph_enabled",
  "code_index.symbol_summary.enabled",
  "code_index.symbol_summary.batch_size",
  "code_index.symbol_summary.profile",
  "code_index.symbol_summary.candidates",
  "code_index.symbol_summary.max_concurrency",
  "code_index.symbol_summary.max_tokens",
  "code_index.sync_worker_interval_seconds",
  "code_index.sync_worker_batch_size",
  "indexing.respect_gitignore",
  // Binary freshness
  "bin_freshness.enabled",
  "bin_freshness.initial_delay_seconds",
  "bin_freshness.interval_seconds",
  "bin_freshness.jitter_seconds",
  "bin_freshness.github_timeout_seconds",
  // Digest
  "digest.enabled",
  "digest.profile",
  "digest.candidates",
  "digest.timeout",
  // Sandboxing
  "web_chat_sandbox.enabled",
  "web_chat_sandbox.extra_read_paths",
  "web_chat_sandbox.extra_write_paths",
  "agent_sandbox.enabled",
  "agent_sandbox.extra_read_paths",
  "agent_sandbox.extra_write_paths",
  // Directories
  "clones_dir",
  "worktrees_dir",
];

// `search.mode` and `ui.mode` are bounded in the backend but persisted as free
// text (no JSON-schema enum), so the section supplies the option set explicitly
// rather than deriving it from the schema.
const SEARCH_MODE_OPTIONS = [
  { value: "keyword", label: "Keyword only" },
  { value: "embedding", label: "Embedding only" },
  { value: "auto", label: "Auto (embedding, keyword fallback)" },
  { value: "hybrid", label: "Hybrid (weighted blend)" },
];

const UI_MODE_OPTIONS = [
  { value: "auto", label: "Auto" },
  { value: "production", label: "Production (serve built assets)" },
  { value: "dev", label: "Dev (proxy the Vite dev server)" },
];

function DaemonGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Daemon"
      hint="HTTP listener, health checks, and the origins allowed to call the daemon."
    >
      <NumberConfigField
        fields={fields}
        path="daemon_port"
        label="HTTP port"
        ariaLabel="Daemon HTTP port"
      />
      <TextConfigField
        fields={fields}
        path="bind_host"
        label="Bind host"
        ariaLabel="Daemon bind host"
        placeholder="127.0.0.1"
      />
      <NumberConfigField
        fields={fields}
        path="daemon_health_check_interval"
        label="Health-check interval (seconds)"
        ariaLabel="Daemon health-check interval (seconds)"
      />
      <SwitchConfigField
        fields={fields}
        path="test_mode"
        label="Test mode"
        ariaLabel="Enable test mode"
      />
      <StringListConfigField
        fields={fields}
        path="cors_origins"
        label="CORS origins"
        ariaLabel="CORS origin"
        addLabel="Add CORS origin"
        placeholder="https://app.example.com"
      />
    </Subsection>
  );
}

function WebSocketGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="WebSocket"
      hint="Realtime event channel for the UI and connected clients."
    >
      <SwitchConfigField
        fields={fields}
        path="websocket.enabled"
        label="Enable WebSocket server"
        ariaLabel="Enable WebSocket server"
      />
      <NumberConfigField
        fields={fields}
        path="websocket.port"
        label="Port"
        ariaLabel="WebSocket port"
      />
      <NumberConfigField
        fields={fields}
        path="websocket.ping_interval"
        label="Ping interval (seconds)"
        ariaLabel="WebSocket ping interval (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="websocket.ping_timeout"
        label="Ping timeout (seconds)"
        ariaLabel="WebSocket ping timeout (seconds)"
      />
    </Subsection>
  );
}

function WebUiGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Web UI serving"
      hint="How the daemon serves this interface and the graph display limits."
    >
      <SwitchConfigField
        fields={fields}
        path="ui.enabled"
        label="Enable web UI serving"
        ariaLabel="Enable web UI serving"
      />
      <OptionsSelectField
        fields={fields}
        path="ui.mode"
        label="Serving mode"
        ariaLabel="Web UI mode"
        options={UI_MODE_OPTIONS}
      />
      <NumberConfigField
        fields={fields}
        path="ui.port"
        label="Dev server port"
        ariaLabel="Web UI dev port"
      />
      <TextConfigField
        fields={fields}
        path="ui.host"
        label="Dev server host"
        ariaLabel="Web UI dev host"
        placeholder="localhost"
      />
      <TextConfigField
        fields={fields}
        path="ui.web_dir"
        label="Web directory"
        ariaLabel="Web directory path"
        placeholder="Auto-detected when blank"
      />
      <NumberConfigField
        fields={fields}
        path="ui.knowledge_graph_limit"
        label="Knowledge graph entity limit"
        ariaLabel="Knowledge graph entity limit"
      />
      <NumberConfigField
        fields={fields}
        path="ui.knowledge_graph_relationship_limit"
        label="Knowledge graph relationship limit"
        ariaLabel="Knowledge graph relationship limit"
      />
    </Subsection>
  );
}

function SearchGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Search"
      hint="Mode and hybrid weighting for unified keyword/embedding search."
    >
      <OptionsSelectField
        fields={fields}
        path="search.mode"
        label="Mode"
        ariaLabel="Search mode"
        options={SEARCH_MODE_OPTIONS}
      />
      <NumberConfigField
        fields={fields}
        path="search.keyword_weight"
        label="Keyword weight"
        ariaLabel="Search keyword weight"
        step={0.1}
      />
      <NumberConfigField
        fields={fields}
        path="search.embedding_weight"
        label="Embedding weight"
        ariaLabel="Search embedding weight"
        step={0.1}
      />
      <SwitchConfigField
        fields={fields}
        path="search.notify_on_fallback"
        label="Warn when search falls back to keyword"
        ariaLabel="Warn when search falls back to keyword"
      />
    </Subsection>
  );
}

function CodeIndexGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Code index"
      hint="Symbol/graph indexing, embeddings, summaries, and the background sync worker."
    >
      <SwitchConfigField
        fields={fields}
        path="code_index.enabled"
        label="Enable code index"
        ariaLabel="Enable code index"
      />
      <NumberConfigField
        fields={fields}
        path="code_index.maintenance_interval_seconds"
        label="Maintenance interval (seconds)"
        ariaLabel="Index maintenance interval (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="code_index.maintenance_index_timeout_seconds"
        label="Maintenance index timeout (seconds)"
        ariaLabel="Maintenance index timeout (seconds)"
      />
      <SwitchConfigField
        fields={fields}
        path="code_index.nightly_repair_enabled"
        label="Nightly index repair"
        ariaLabel="Nightly index repair"
      />
      <TextConfigField
        fields={fields}
        path="code_index.nightly_repair_cron"
        label="Nightly index repair cron"
        ariaLabel="Nightly index repair cron"
        placeholder="0 2 * * *"
      />
      <TextConfigField
        fields={fields}
        path="code_index.nightly_repair_timezone"
        label="Nightly index repair timezone"
        ariaLabel="Nightly index repair timezone"
        placeholder="UTC"
        nullable
      />
      <NumberConfigField
        fields={fields}
        path="code_index.nightly_repair_timeout_seconds"
        label="Nightly index repair timeout (seconds)"
        ariaLabel="Nightly index repair timeout (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="code_index.nightly_repair_concurrency"
        label="Nightly index repair concurrency"
        ariaLabel="Nightly index repair concurrency"
      />
      <TextConfigField
        fields={fields}
        path="code_index.maintenance_log_file"
        label="Maintenance log file"
        ariaLabel="Maintenance log file"
        placeholder="~/.gobby/logs/code-index-maintenance.log"
      />
      <NumberConfigField
        fields={fields}
        path="code_index.missing_root_purge_observations"
        label="Missing-root purge threshold (observations)"
        ariaLabel="Missing-root purge threshold (observations)"
      />
      <SwitchConfigField
        fields={fields}
        path="code_index.embedding_enabled"
        label="Enable code embeddings"
        ariaLabel="Enable code embeddings"
      />
      <SwitchConfigField
        fields={fields}
        path="code_index.graph_enabled"
        label="Enable code graph"
        ariaLabel="Enable code graph"
      />
      <SwitchConfigField
        fields={fields}
        path="code_index.symbol_summary.enabled"
        label="Enable code summaries"
        ariaLabel="Enable code summaries"
      />
      <NumberConfigField
        fields={fields}
        path="code_index.symbol_summary.batch_size"
        label="Summary batch size"
        ariaLabel="Summary batch size"
      />
      <SchemaSelectField
        fields={fields}
        path="code_index.symbol_summary.profile"
        label="Summary capability profile"
        ariaLabel="Code summary capability profile"
      />
      <StringListConfigField
        fields={fields}
        path="code_index.symbol_summary.candidates"
        label="Summary provider/model candidates"
        ariaLabel="Summary candidate"
        addLabel="Add summary candidate"
        placeholder="provider/model"
      />
      <NumberConfigField
        fields={fields}
        path="code_index.symbol_summary.max_concurrency"
        label="Summary max concurrency"
        ariaLabel="Summary max concurrency"
      />
      <NumberConfigField
        fields={fields}
        path="code_index.symbol_summary.max_tokens"
        label="Summary max tokens"
        ariaLabel="Summary max tokens"
      />
      <NumberConfigField
        fields={fields}
        path="code_index.sync_worker_interval_seconds"
        label="Sync worker interval (seconds)"
        ariaLabel="Sync worker interval (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="code_index.sync_worker_batch_size"
        label="Sync worker batch size"
        ariaLabel="Sync worker batch size"
      />
      <SwitchConfigField
        fields={fields}
        path="indexing.respect_gitignore"
        label="Respect .gitignore when indexing"
        ariaLabel="Respect .gitignore when indexing"
      />
    </Subsection>
  );
}

function BinFreshnessGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Binary freshness"
      hint="Background checks that compare the installed binary against the latest release."
    >
      <SwitchConfigField
        fields={fields}
        path="bin_freshness.enabled"
        label="Enable binary freshness checks"
        ariaLabel="Enable binary freshness checks"
      />
      <NumberConfigField
        fields={fields}
        path="bin_freshness.initial_delay_seconds"
        label="Initial delay (seconds)"
        ariaLabel="Freshness initial delay (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="bin_freshness.interval_seconds"
        label="Check interval (seconds)"
        ariaLabel="Freshness interval (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="bin_freshness.jitter_seconds"
        label="Jitter (seconds)"
        ariaLabel="Freshness jitter (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="bin_freshness.github_timeout_seconds"
        label="GitHub request timeout (seconds)"
        ariaLabel="GitHub request timeout (seconds)"
      />
    </Subsection>
  );
}

function DigestGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Digest"
      hint="Periodic LLM digest generation and its capability profile."
    >
      <SwitchConfigField
        fields={fields}
        path="digest.enabled"
        label="Enable digest generation"
        ariaLabel="Enable digest generation"
      />
      <SchemaSelectField
        fields={fields}
        path="digest.profile"
        label="Capability profile"
        ariaLabel="Digest capability profile"
      />
      <StringListConfigField
        fields={fields}
        path="digest.candidates"
        label="Provider/model candidates"
        ariaLabel="Digest candidate"
        addLabel="Add digest candidate"
        placeholder="provider/model"
      />
      <NumberConfigField
        fields={fields}
        path="digest.timeout"
        label="Timeout (seconds)"
        ariaLabel="Digest timeout (seconds)"
      />
    </Subsection>
  );
}

function WebChatSandboxGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Web chat sandbox"
      hint="Filesystem boundary for tools invoked from web chat."
    >
      <SwitchConfigField
        fields={fields}
        path="web_chat_sandbox.enabled"
        label="Enable web chat sandbox"
        ariaLabel="Enable web chat sandbox"
      />
      <StringListConfigField
        fields={fields}
        path="web_chat_sandbox.extra_read_paths"
        label="Extra read paths"
        ariaLabel="Web chat read path"
        addLabel="Add web chat read path"
        placeholder="/absolute/path"
      />
      <StringListConfigField
        fields={fields}
        path="web_chat_sandbox.extra_write_paths"
        label="Extra write paths"
        ariaLabel="Web chat write path"
        addLabel="Add web chat write path"
        placeholder="/absolute/path"
      />
    </Subsection>
  );
}

function AgentSandboxGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Agent sandbox"
      hint="Filesystem boundary for spawned agents."
    >
      <SwitchConfigField
        fields={fields}
        path="agent_sandbox.enabled"
        label="Enable agent sandbox"
        ariaLabel="Enable agent sandbox"
      />
      <StringListConfigField
        fields={fields}
        path="agent_sandbox.extra_read_paths"
        label="Extra read paths"
        ariaLabel="Agent read path"
        addLabel="Add agent read path"
        placeholder="/absolute/path"
      />
      <StringListConfigField
        fields={fields}
        path="agent_sandbox.extra_write_paths"
        label="Extra write paths"
        ariaLabel="Agent write path"
        addLabel="Add agent write path"
        placeholder="/absolute/path"
      />
    </Subsection>
  );
}

function DirectoriesGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Directories"
      hint="Where clone and worktree isolation roots are created."
    >
      <TextConfigField
        fields={fields}
        path="clones_dir"
        label="Clones directory"
        ariaLabel="Clones directory"
        placeholder="~/.gobby/clones"
      />
      <TextConfigField
        fields={fields}
        path="worktrees_dir"
        label="Worktrees directory"
        ariaLabel="Worktrees directory"
        placeholder="~/.gobby/worktrees"
      />
    </Subsection>
  );
}

/**
 * Runtime & Infrastructure: daemon ports and CORS, the WebSocket server, web UI
 * serving, unified search, the code index, binary freshness, digest generation,
 * sandbox boundaries, and isolation directories — all DaemonConfig rows saved
 * through the section's draft Save/Discard footer.
 */
export function RuntimeInfrastructureSection() {
  return (
    <SettingsSection
      sectionId="runtime-infrastructure"
      ownedPaths={OWNED_PATHS}
    >
      {(fields) => (
        <>
          <DaemonGroup fields={fields} />
          <WebSocketGroup fields={fields} />
          <WebUiGroup fields={fields} />
          <SearchGroup fields={fields} />
          <CodeIndexGroup fields={fields} />
          <BinFreshnessGroup fields={fields} />
          <DigestGroup fields={fields} />
          <WebChatSandboxGroup fields={fields} />
          <AgentSandboxGroup fields={fields} />
          <DirectoriesGroup fields={fields} />
        </>
      )}
    </SettingsSection>
  );
}
