import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { RuntimeInfrastructureSection } from "../RuntimeInfrastructureSection";
import {
  SettingsSectionContext,
  type SettingsSectionContextValue,
} from "../SettingsSectionContext";

// Minimal schema covering the rows the assertions touch: the two `*.profile`
// rows reach the shared FeatureProfile enum through a `$ref`, and a couple of
// bounded numbers carry their schema min/max — mirroring the real DaemonConfig
// shape. The free-text-bounded selects (search.mode / ui.mode) take explicit
// options from the section, so they need no schema enum here.
const SCHEMA: Record<string, unknown> = {
  $defs: {
    FeatureProfile: {
      enum: ["feature_low", "feature_mid", "feature_high"],
      type: "string",
    },
    CodeIndexConfig: {
      type: "object",
      properties: {
        symbol_summary: { $ref: "#/$defs/CodeIndexSymbolSummaryConfig" },
      },
    },
    CodeIndexSymbolSummaryConfig: {
      type: "object",
      properties: {
        enabled: { type: "boolean" },
        batch_size: { type: "integer", minimum: 1 },
        profile: { $ref: "#/$defs/FeatureProfile" },
        candidates: { type: "array", items: { type: "string" } },
        max_concurrency: { type: "integer", minimum: 1 },
        max_tokens: { type: "integer", minimum: 1 },
      },
    },
    UIConfig: {
      type: "object",
      properties: {
        knowledge_graph_limit: { type: "integer", minimum: 0 },
      },
    },
  },
  type: "object",
  properties: {
    code_index: { $ref: "#/$defs/CodeIndexConfig" },
    ui: { $ref: "#/$defs/UIConfig" },
  },
};

function makeConfigValues(): Record<string, unknown> {
  return {
    daemon_port: 60887,
    bind_host: "127.0.0.1",
    daemon_health_check_interval: 30,
    test_mode: false,
    cors_origins: ["http://localhost:3000", "http://localhost:5173"],
    clones_dir: "/home/dev/.gobby/clones",
    worktrees_dir: "/home/dev/.gobby/worktrees",
    websocket: {
      enabled: true,
      port: 60888,
      ping_interval: 20,
      ping_timeout: 20,
    },
    ui: {
      enabled: false,
      mode: "auto",
      port: 60889,
      host: "localhost",
      web_dir: "/home/dev/gobby/web",
      knowledge_graph_limit: 500,
      knowledge_graph_relationship_limit: 2000,
    },
    search: {
      mode: "auto",
      keyword_weight: 0.4,
      embedding_weight: 0.6,
      notify_on_fallback: true,
    },
    code_index: {
      enabled: true,
      maintenance_interval_seconds: 3600,
      maintenance_index_timeout_seconds: 900,
      nightly_repair_enabled: true,
      nightly_repair_cron: "0 2 * * *",
      nightly_repair_timezone: null,
      nightly_repair_timeout_seconds: 7200,
      nightly_repair_concurrency: 1,
      maintenance_log_file: "~/.gobby/logs/code-index-maintenance.log",
      missing_root_purge_observations: 3,
      embedding_enabled: true,
      graph_enabled: true,
      symbol_summary: {
        enabled: false,
        batch_size: 16,
        profile: "feature_low",
        candidates: ["anthropic/claude-haiku"],
        max_concurrency: 4,
        max_tokens: 100,
      },
      sync_worker_interval_seconds: 30,
      sync_worker_batch_size: 50,
    },
    indexing: {
      respect_gitignore: true,
    },
    bin_freshness: {
      enabled: true,
      initial_delay_seconds: 60,
      interval_seconds: 3600,
      jitter_seconds: 30,
      github_timeout_seconds: 10,
    },
    web_chat_sandbox: {
      enabled: true,
      extra_read_paths: ["/srv/shared"],
      extra_write_paths: ["/srv/out"],
    },
    agent_sandbox: {
      enabled: false,
      extra_read_paths: ["/srv/agent-read"],
      extra_write_paths: ["/srv/agent-write"],
    },
  };
}

function makeContext(
  overrides: Partial<SettingsSectionContextValue> = {},
): SettingsSectionContextValue {
  return {
    schema: SCHEMA,
    configValues: makeConfigValues(),
    secretKeys: [],
    isLoading: false,
    saveConfig: vi.fn(async () => ({ ok: true })),
    registerDirtyGuard: () => () => {},
    ...overrides,
  };
}

function renderSection(ctx: SettingsSectionContextValue) {
  return render(
    <SettingsSectionContext.Provider value={ctx}>
      <RuntimeInfrastructureSection />
    </SettingsSectionContext.Provider>,
  );
}

describe("RuntimeInfrastructureSection", () => {
  it("reads the daemon core rows", () => {
    renderSection(makeContext());

    expect(screen.getByLabelText("Daemon HTTP port")).toHaveValue(60887);
    expect(screen.getByLabelText("Daemon bind host")).toHaveValue("127.0.0.1");
    expect(
      screen.getByLabelText("Daemon health-check interval (seconds)"),
    ).toHaveValue(30);
    expect(
      screen.getByRole("switch", { name: "Enable test mode" }),
    ).not.toBeChecked();
  });

  it("renders cors_origins as an editable string list, not a text fallback", () => {
    renderSection(makeContext());

    expect(screen.getByLabelText("CORS origin item 1")).toHaveValue(
      "http://localhost:3000",
    );
    expect(screen.getByLabelText("CORS origin item 2")).toHaveValue(
      "http://localhost:5173",
    );
  });

  it("reads the websocket rows", () => {
    renderSection(makeContext());

    expect(
      screen.getByRole("switch", { name: "Enable WebSocket server" }),
    ).toBeChecked();
    expect(screen.getByLabelText("WebSocket port")).toHaveValue(60888);
    expect(
      screen.getByLabelText("WebSocket ping interval (seconds)"),
    ).toHaveValue(20);
    expect(
      screen.getByLabelText("WebSocket ping timeout (seconds)"),
    ).toHaveValue(20);
  });

  it("renders search.mode as a bounded select, not free text", () => {
    renderSection(makeContext());

    const mode = screen.getByLabelText("Search mode");
    expect(mode).toHaveValue("auto");
    expect(within(mode).getAllByRole("option")).toHaveLength(4);
    expect(screen.getByLabelText("Search keyword weight")).toHaveValue(0.4);
    expect(screen.getByLabelText("Search embedding weight")).toHaveValue(0.6);
    expect(
      screen.getByRole("switch", {
        name: "Warn when search falls back to keyword",
      }),
    ).toBeChecked();
  });

  it("renders ui.mode as a bounded select with the serving rows", () => {
    renderSection(makeContext());

    const mode = screen.getByLabelText("Web UI mode");
    expect(mode).toHaveValue("auto");
    expect(within(mode).getAllByRole("option")).toHaveLength(3);
    expect(
      screen.getByRole("switch", { name: "Enable web UI serving" }),
    ).not.toBeChecked();
    expect(screen.getByLabelText("Web UI dev port")).toHaveValue(60889);
    expect(screen.getByLabelText("Web UI dev host")).toHaveValue("localhost");
    expect(screen.getByLabelText("Web directory path")).toHaveValue(
      "/home/dev/gobby/web",
    );
    expect(screen.getByLabelText("Knowledge graph entity limit")).toHaveValue(
      500,
    );
    expect(
      screen.getByLabelText("Knowledge graph relationship limit"),
    ).toHaveValue(2000);
    expect(
      screen.queryByLabelText("Memory graph node limit"),
    ).not.toBeInTheDocument();
  });

  it("renders kept code-index summary fields and omits retired controls", () => {
    renderSection(makeContext());

    expect(screen.getByLabelText("Summary candidate item 1")).toHaveValue(
      "anthropic/claude-haiku",
    );
    const profile = screen.getByLabelText("Code summary capability profile");
    expect(profile).toHaveValue("feature_low");
    expect(within(profile).getAllByRole("option")).toHaveLength(3);
    expect(
      screen.getByRole("switch", { name: "Nightly index repair" }),
    ).toBeChecked();
    expect(screen.getByLabelText("Nightly index repair cron")).toHaveValue(
      "0 2 * * *",
    );
    expect(
      screen.getByLabelText("Nightly index repair timeout (seconds)"),
    ).toHaveValue(7200);
    expect(
      screen.queryByLabelText("Re-index on commit"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Maximum indexed file size (bytes)"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Qdrant collection prefix"),
    ).not.toBeInTheDocument();
  });

  it("renders the sandbox path editors as string lists", () => {
    renderSection(makeContext());

    expect(screen.getByLabelText("Web chat read path item 1")).toHaveValue(
      "/srv/shared",
    );
    expect(screen.getByLabelText("Web chat write path item 1")).toHaveValue(
      "/srv/out",
    );
    expect(screen.getByLabelText("Agent read path item 1")).toHaveValue(
      "/srv/agent-read",
    );
    expect(screen.getByLabelText("Agent write path item 1")).toHaveValue(
      "/srv/agent-write",
    );
  });

  it("reads the binary-freshness and directory rows", () => {
    renderSection(makeContext());

    expect(
      screen.getByRole("switch", { name: "Enable binary freshness checks" }),
    ).toBeChecked();
    expect(screen.getByLabelText("Freshness interval (seconds)")).toHaveValue(
      3600,
    );
    expect(screen.getByLabelText("Clones directory")).toHaveValue(
      "/home/dev/.gobby/clones",
    );
    expect(screen.getByLabelText("Worktrees directory")).toHaveValue(
      "/home/dev/.gobby/worktrees",
    );
  });

  it("persists an edited scalar through the section Save", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    fireEvent.click(screen.getByRole("switch", { name: "Enable test mode" }));
    const save = screen.getByRole("button", { name: "Save" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1));
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ test_mode: true }),
    );
  });

  it("persists an edited cors_origins list through the section Save", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    fireEvent.change(screen.getByLabelText("CORS origin item 1"), {
      target: { value: "http://localhost:4000" },
    });
    const save = screen.getByRole("button", { name: "Save" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1));
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        cors_origins: ["http://localhost:4000", "http://localhost:5173"],
      }),
    );
  });

  it("persists a bounded-select change through the section Save", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    fireEvent.change(screen.getByLabelText("Search mode"), {
      target: { value: "hybrid" },
    });
    const save = screen.getByRole("button", { name: "Save" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1));
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ "search.mode": "hybrid" }),
    );
  });

  it("does not render the legacy pending placeholder", () => {
    renderSection(makeContext());

    expect(
      screen.queryByText(/being migrated into this section/),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Code index")).toBeInTheDocument();
  });
});
