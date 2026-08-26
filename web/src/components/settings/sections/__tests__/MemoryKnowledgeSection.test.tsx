import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { MemoryKnowledgeSection } from "../MemoryKnowledgeSection";
import {
  SettingsSectionContext,
  type SettingsSectionContextValue,
} from "../SettingsSectionContext";

/**
 * The generated runtime config contract is the live registry of every daemon
 * config key. Reading it here — instead of trusting the hand-written SCHEMA
 * fixture below — is what turns a backend field the daemon removed or added
 * into a frontend failure, rather than a settings row bound to nothing.
 */
const CONTRACT_REL = "crates/gcore/assets/config/runtime_config_contract.json";

/** Resolve a repo-root file whether vitest runs from `web/` or the repo root. */
function resolveRepoFile(relative: string): string {
  let dir = process.cwd();
  for (;;) {
    const candidate = resolve(dir, relative);
    if (existsSync(candidate)) return candidate;
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  throw new Error(`Could not locate ${relative} from cwd ${process.cwd()}`);
}

const CONTRACT_KEYS: ReadonlySet<string> = new Set(
  (
    JSON.parse(readFileSync(resolveRepoFile(CONTRACT_REL), "utf8")) as {
      exactKeys: ReadonlyArray<{ key: string }>;
    }
  ).exactKeys.map((entry) => entry.key),
);

// Schema covering the rows the assertions touch. The two `profile` selects
// (`memory.kg.profile`, `memory.dream.profile`) prove
// multi-hop `$ref` traversal through the real DaemonConfig shape down to the
// shared `FeatureProfile` enum; `candidates`, `wiki.roots`, and `ignore_globs`
// cover the array "fix" rows from the configuration audit.
const FEATURE_PROFILE = {
  enum: ["feature_low", "feature_mid", "feature_high"],
  type: "string",
};

const SCHEMA: Record<string, unknown> = {
  $defs: {
    FeatureProfile: FEATURE_PROFILE,
    MemoryKnowledgeGraphConfig: {
      type: "object",
      properties: {
        profile: { $ref: "#/$defs/FeatureProfile" },
        candidates: { type: "array", items: { type: "string" } },
      },
    },
    MemoryDreamConfig: {
      type: "object",
      properties: {
        profile: { $ref: "#/$defs/FeatureProfile" },
        candidates: { type: "array", items: { type: "string" } },
        enabled: { type: "boolean" },
        schedule_cron: { type: "string" },
      },
    },
    MemoryConfig: {
      type: "object",
      properties: {
        enabled: { type: "boolean" },
        backend: { type: "string" },
        auto_crossref: { type: "boolean" },
        crossref_threshold: { type: "number" },
        kg: { $ref: "#/$defs/MemoryKnowledgeGraphConfig" },
        dream: { $ref: "#/$defs/MemoryDreamConfig" },
        recall_signal_logging: { type: "boolean" },
        recall_signal_log_path: {
          anyOf: [{ type: "string" }, { type: "null" }],
        },
      },
    },
    // The embedding-switch structural keys carry managed activation: the
    // store rejects direct writes, so the section must exclude them from the
    // save payload and route catalog-key edits through the managed action.
    EmbeddingsConfig: {
      type: "object",
      properties: {
        model: { type: "string", activation: "managed" },
        dim: { type: "integer", activation: "managed" },
        api_base: {
          anyOf: [{ type: "string" }, { type: "null" }],
          activation: "managed",
        },
        api_key: { anyOf: [{ type: "string" }, { type: "null" }] },
        query_prefix: {
          anyOf: [{ type: "string" }, { type: "null" }],
          activation: "managed",
        },
        catalog_key: { type: "string", activation: "managed" },
      },
    },
    QdrantConfig: {
      type: "object",
      properties: {
        url: { anyOf: [{ type: "string" }, { type: "null" }] },
        api_key: { anyOf: [{ type: "string" }, { type: "null" }] },
        port: { type: "integer" },
        collection_prefix: { type: "string" },
      },
    },
    FalkorConfig: {
      type: "object",
      properties: {
        host: { type: "string" },
        port: { type: "integer" },
        password: { anyOf: [{ type: "string" }, { type: "null" }] },
        graph_name: { type: "string" },
        graph_search: { type: "boolean" },
        graph_min_score: { type: "number" },
        rrf_k: { type: "integer" },
      },
    },
    DatabasesConfig: {
      type: "object",
      properties: {
        qdrant: { $ref: "#/$defs/QdrantConfig" },
        falkordb: { $ref: "#/$defs/FalkorConfig" },
      },
    },
    KnowledgeGraphQueueConfig: {
      type: "object",
      properties: {
        interval_minutes: { type: "integer" },
        batch_size: { type: "integer" },
      },
    },
    MemoryBackupConfig: {
      type: "object",
      properties: {
        enabled: { type: "boolean" },
        backup_path: { type: "string" },
      },
    },
    WikiRootConfig: {
      type: "object",
      properties: {
        scope: { type: "string" },
        path: { type: "string" },
      },
      required: ["scope", "path"],
    },
    WikiConfig: {
      type: "object",
      properties: {
        enabled: { type: "boolean" },
        roots: { type: "array", items: { $ref: "#/$defs/WikiRootConfig" } },
        debounce_interval: { type: "number" },
        poll_interval: { type: "number" },
        ignore_globs: { type: "array", items: { type: "string" } },
        codewiki_on_commit: { type: "boolean" },
        codewiki_nightly_enabled: { type: "boolean" },
        codewiki_nightly_schedule_cron: { type: "string" },
        codewiki_nightly_timezone: {
          anyOf: [{ type: "string" }, { type: "null" }],
        },
      },
    },
  },
  type: "object",
  properties: {
    memory: { $ref: "#/$defs/MemoryConfig" },
    ai: {
      type: "object",
      properties: { embeddings: { $ref: "#/$defs/EmbeddingsConfig" } },
    },
    databases: { $ref: "#/$defs/DatabasesConfig" },
    knowledge_graph_queue: { $ref: "#/$defs/KnowledgeGraphQueueConfig" },
    memory_backup: { $ref: "#/$defs/MemoryBackupConfig" },
    wiki: { $ref: "#/$defs/WikiConfig" },
  },
};

function makeConfigValues(): Record<string, unknown> {
  return {
    memory: {
      enabled: true,
      backend: "local",
      auto_crossref: true,
      crossref_threshold: 0.7,
      crossref_max_links: 5,
      access_debounce_seconds: 2,
      kg: { profile: "feature_low", candidates: ["claude/haiku"] },
      dream: {
        profile: "feature_mid",
        candidates: ["codex/gpt-5.4-mini"],
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
      },
      code_link_min_score: 0.5,
      temporal_decay_half_life_days: 30,
      graph_edge_weighting: true,
      materialize_cooccurrence: false,
      graph_edge_decay: true,
      edge_half_life_days: 14,
      recall_signal_logging: false,
      recall_signal_log_path: null,
    },
    ai: {
      embeddings: {
        model: "text-embedding-3-small",
        dim: 1536,
        api_base: "https://api.example/v1",
        api_key: "sk-secret",
        query_prefix: "query: ",
        catalog_key: "openai/text-embedding-3-small",
      },
    },
    databases: {
      qdrant: {
        url: "http://localhost:6333",
        api_key: "qdrant-secret",
        port: 6333,
        collection_prefix: "gobby_",
      },
      falkordb: {
        host: "localhost",
        port: 6379,
        password: "falkor-secret",
        graph_name: "gobby",
        graph_search: true,
        graph_min_score: 0.1,
        rrf_k: 60,
      },
    },
    knowledge_graph_queue: { interval_minutes: 5, batch_size: 25 },
    memory_backup: {
      enabled: true,
      backup_path: ".gobby/memories.jsonl",
    },
    wiki: {
      enabled: true,
      roots: [{ scope: "project", path: "docs/wiki" }],
      debounce_interval: 0.5,
      poll_interval: 0.25,
      ignore_globs: ["outputs/**"],
      codewiki_on_commit: false,
      codewiki_nightly_enabled: false,
      codewiki_nightly_schedule_cron: "0 3 * * *",
      codewiki_nightly_timezone: null,
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
      <MemoryKnowledgeSection />
    </SettingsSectionContext.Provider>,
  );
}

describe("MemoryKnowledgeSection", () => {
  it("reads core memory scalar rows", () => {
    renderSection(makeContext());

    expect(screen.getByRole("switch", { name: "Enable memory" })).toBeChecked();
    expect(screen.getByLabelText("Cross-reference threshold")).toHaveValue(0.7);
    expect(
      screen.getByRole("switch", { name: "Log recall signals" }),
    ).not.toBeChecked();
  });

  it("renders memory backend as a bounded local/null select", () => {
    renderSection(makeContext());

    const backend = screen.getByLabelText("Memory backend");
    expect(backend).toHaveValue("local");
    expect(within(backend).getAllByRole("option")).toHaveLength(2);
  });

  it("renders only rows for keys the config contract registers", () => {
    // Guard against a vacuous pass if the contract moves or its shape changes.
    expect(CONTRACT_KEYS.has("memory.crossref_threshold")).toBe(true);
    expect(CONTRACT_KEYS.size).toBeGreaterThan(100);

    const { container } = renderSection(makeContext());
    const rendered = [...container.querySelectorAll("[data-config-path]")]
      .map((node) => node.getAttribute("data-config-path"))
      .filter((path): path is string => path !== null);

    // A row bound to a key the daemon dropped edits nothing; the recall rows
    // retired with MemoryRecallConfig (#21009, #21022) are the precedent.
    expect(rendered.length).toBeGreaterThan(0);
    expect(rendered.filter((path) => !CONTRACT_KEYS.has(path))).toEqual([]);
  });

  it("resolves the knowledge-graph and dream profile enums", () => {
    renderSection(makeContext());

    expect(screen.getByLabelText("Knowledge graph model profile")).toHaveValue(
      "feature_low",
    );
    expect(screen.getByLabelText("Dream model profile")).toHaveValue(
      "feature_mid",
    );
  });

  it("renders candidate arrays as editable string lists", () => {
    renderSection(makeContext());

    expect(
      screen.getByLabelText("Knowledge graph model candidates item 1"),
    ).toHaveValue("claude/haiku");
    expect(screen.getByLabelText("Dream model candidates item 1")).toHaveValue(
      "codex/gpt-5.4-mini",
    );
  });

  it("reads embeddings, vector store, and graph store rows", () => {
    renderSection(makeContext());

    expect(screen.getByLabelText("Embedding model")).toHaveValue(
      "text-embedding-3-small",
    );
    expect(screen.getByLabelText("Embedding dimensions")).toHaveValue(1536);
    expect(screen.getByLabelText("Embedding catalog key")).toHaveValue(
      "openai/text-embedding-3-small",
    );
    expect(screen.getByLabelText("Qdrant URL")).toHaveValue(
      "http://localhost:6333",
    );
    expect(screen.getByLabelText("Qdrant port")).toHaveValue(6333);
    expect(screen.getByLabelText("FalkorDB host")).toHaveValue("localhost");
    expect(
      screen.getByRole("switch", { name: "FalkorDB graph search" }),
    ).toBeChecked();
    expect(screen.getByLabelText("FalkorDB RRF k")).toHaveValue(60);
  });

  it("routes secret credentials to Secrets & Auth (not rendered here)", () => {
    renderSection(makeContext());

    expect(screen.queryByLabelText("Embedding API key")).toBeNull();
    expect(screen.queryByLabelText("Qdrant API key")).toBeNull();
    expect(screen.queryByLabelText("FalkorDB password")).toBeNull();
  });

  it("reads knowledge-graph queue and memory backup rows", () => {
    renderSection(makeContext());

    expect(screen.getByLabelText("Queue interval (minutes)")).toHaveValue(5);
    expect(screen.getByLabelText("Queue batch size")).toHaveValue(25);
    expect(
      screen.getByRole("switch", { name: "Enable memory backup" }),
    ).toBeChecked();
    expect(screen.getByLabelText("Memory backup path")).toHaveValue(
      ".gobby/memories.jsonl",
    );
  });

  it("renders wiki.roots as a typed scope/path sub-form", () => {
    renderSection(makeContext());

    expect(screen.getByLabelText("Wiki root 1 scope")).toHaveValue("project");
    expect(screen.getByLabelText("Wiki root 1 path")).toHaveValue("docs/wiki");
    expect(screen.getByLabelText("Wiki ignore globs item 1")).toHaveValue(
      "outputs/**",
    );
    expect(
      screen.queryByRole("switch", { name: "Refresh codewiki on commit" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("switch", { name: "Refresh codewiki nightly" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Nightly codewiki refresh schedule"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Nightly codewiki refresh timezone"),
    ).not.toBeInTheDocument();
  });

  it("persists an edited draft row through the section Save", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    fireEvent.click(screen.getByRole("switch", { name: "Enable memory" }));
    const save = screen.getByRole("button", { name: "Save" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1));
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ "memory.enabled": false }),
    );
  });

  it("excludes managed-activation paths from the save payload", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    fireEvent.click(screen.getByRole("switch", { name: "Enable memory" }));
    const save = screen.getByRole("button", { name: "Save" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(ctx.saveConfig).mock.calls[0][0];
    expect(payload["memory.enabled"]).toBe(false);
    // The store rejects direct writes to the embedding switch's structural
    // keys; submitting them would brick the whole section save.
    for (const managedPath of [
      "ai.embeddings.model",
      "ai.embeddings.dim",
      "ai.embeddings.api_base",
      "ai.embeddings.query_prefix",
      "ai.embeddings.catalog_key",
    ]) {
      expect(payload).not.toHaveProperty([managedPath]);
    }
    // Non-managed owned rows still ride along with the section save.
    expect(payload).toHaveProperty(["databases.qdrant.port"], 6333);
  });

  it("catalog-key edits update the row without dirtying the draft", () => {
    const ctx = makeContext();
    renderSection(ctx);

    const catalogKey = screen.getByLabelText("Embedding catalog key");
    fireEvent.change(catalogKey, {
      target: { value: "ollama/qwen3-embedding" },
    });

    // The managed action drives local state, so the row reflects the pending
    // selection while the Model row keeps showing the stored draft value.
    expect(catalogKey).toHaveValue("ollama/qwen3-embedding");
    expect(screen.getByLabelText("Embedding model")).toHaveValue(
      "text-embedding-3-small",
    );
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("starts the embedding switch through the managed action", async () => {
    const runManagedAction = vi.fn(async () => true);
    renderSection(makeContext({ runManagedAction }));

    const start = screen.getByRole("button", {
      name: "Start embedding switch",
    });
    expect(start).toBeDisabled();
    expect(start.parentElement).toHaveClass("flex", "flex-wrap", "gap-2");

    fireEvent.change(screen.getByLabelText("Embedding catalog key"), {
      target: { value: "ollama/qwen3-embedding" },
    });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);

    await waitFor(() =>
      expect(runManagedAction).toHaveBeenCalledWith(
        "/api/embeddings/switch/start",
        { catalog_key: "ollama/qwen3-embedding" },
      ),
    );
  });
});
