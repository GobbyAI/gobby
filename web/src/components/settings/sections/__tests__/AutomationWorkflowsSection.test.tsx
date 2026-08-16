import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { AutomationWorkflowsSection } from "../AutomationWorkflowsSection";
import {
  SettingsSectionContext,
  type SettingsSectionContextValue,
} from "../SettingsSectionContext";

// The variable-defaults editor drives its own live API (useVariableDefs); the
// section test only needs to prove the group is mounted, so stub it to a
// deterministic sentinel. The editor has its own dedicated test.
vi.mock("../../VariableDefaultsEditor", () => ({
  VariableDefaultsEditor: () => <div>variables-editor-sentinel</div>,
}));

// Schema covering the rows the assertions touch. The `gobby-tasks` key (with a
// hyphen) and the two-hop `expansion.profile`/`expansion.default_strategy`
// enums prove nested `$ref` traversal through the real DaemonConfig shape.
const SCHEMA: Record<string, unknown> = {
  $defs: {
    FeatureProfile: {
      enum: ["feature_low", "feature_mid", "feature_high"],
      type: "string",
    },
    TaskExpansionConfig: {
      type: "object",
      properties: {
        profile: { $ref: "#/$defs/FeatureProfile" },
        default_strategy: {
          enum: ["auto", "phased", "sequential", "parallel"],
          type: "string",
        },
      },
    },
    TaskValidationConfig: {
      type: "object",
      properties: {
        escalation_notify: {
          enum: ["webhook", "slack", "none"],
          type: "string",
        },
        max_iterations: { type: "integer", minimum: 1 },
        close_review_prompt_max_chars: { type: "integer", minimum: 1 },
      },
    },
    GobbyTasksConfig: {
      type: "object",
      properties: {
        expansion: { $ref: "#/$defs/TaskExpansionConfig" },
        validation: { $ref: "#/$defs/TaskValidationConfig" },
      },
    },
  },
  type: "object",
  properties: {
    "gobby-tasks": { $ref: "#/$defs/GobbyTasksConfig" },
  },
};

function makeConfigValues(): Record<string, unknown> {
  return {
    "gobby-tasks": {
      enabled: true,
      show_result_on_create: false,
      file_extraction: {
        file_extensions: [".py", ".ts"],
        known_files: ["README.md"],
        path_prefixes: ["src/"],
      },
      expansion: {
        profile: "feature_high",
        candidates: ["claude/sonnet"],
        enabled: true,
        prompt_path: null,
        system_prompt_path: null,
        default_strategy: "auto",
        timeout: 600,
        pattern_criteria: {
          patterns: { backend: ["{unit_tests}", "{type_check}"] },
          detection_keywords: { backend: ["api", "route"] },
        },
      },
      validation: {
        profile: "feature_mid",
        candidates: ["claude/sonnet"],
        enabled: true,
        system_prompt: "Validate the work.",
        prompt_path: null,
        criteria_prompt_path: null,
        criteria_system_prompt: "Generate criteria.",
        max_iterations: 5,
        close_review_prompt_max_chars: 32000,
        escalation_enabled: true,
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
      history_limit: 10000,
      wsl_distribution: null,
      idle_check_enabled: true,
      idle_timeout_seconds: 120,
      idle_reprompt_delay_seconds: 30,
      max_reprompt_attempts: 3,
      reasoning_watchdog_interrupt_enabled: false,
      reasoning_watchdog_settle_seconds: 5,
      init_timeout_seconds: 30,
      init_activity_grace_seconds: 5,
      registration_timeout_seconds: 30,
      auto_enter_approval_prompts: true,
      auto_enter_agent_terminals: false,
      auto_enter_agent_interval_seconds: 10,
    },
    cron: {
      enabled: true,
      check_interval_seconds: 60,
      max_concurrent_jobs: 5,
      running_timeout_seconds: 3600,
      cleanup_after_days: 30,
      backoff_delays: [30, 60, 300],
    },
    system_loops: { automation: { enabled: true, interval_seconds: 30 } },
    pipelines: {
      prompt_step: { profile: "feature_mid", candidates: ["claude/sonnet"] },
      nesting_depth_limit: 3,
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
    rulesEnforcement: true,
    setRulesEnforcement: vi.fn(async () => true),
    ...overrides,
  };
}

function renderSection(ctx: SettingsSectionContextValue) {
  return render(
    <SettingsSectionContext.Provider value={ctx}>
      <AutomationWorkflowsSection />
    </SettingsSectionContext.Provider>,
  );
}

describe("AutomationWorkflowsSection", () => {
  it("reads task-system and file-extraction rows under the hyphenated gobby-tasks key", () => {
    renderSection(makeContext());

    expect(
      screen.getByRole("switch", { name: "Enable task system" }),
    ).toBeChecked();
    expect(
      screen.getByRole("switch", { name: "Show result on task create" }),
    ).not.toBeChecked();
    expect(screen.getByLabelText("Task file extensions item 1")).toHaveValue(
      ".py",
    );
    expect(screen.getByLabelText("Known task files item 1")).toHaveValue(
      "README.md",
    );
  });

  it("resolves expansion enum selects through nested $refs and renders the candidate list", () => {
    renderSection(makeContext());

    const profile = screen.getByLabelText("Expansion capability profile");
    expect(profile).toHaveValue("feature_high");
    expect(within(profile).getAllByRole("option")).toHaveLength(3);

    const strategy = screen.getByLabelText("Default expansion strategy");
    expect(strategy).toHaveValue("auto");
    expect(within(strategy).getAllByRole("option")).toHaveLength(4);

    expect(
      screen.getByLabelText("Expansion model candidate item 1"),
    ).toHaveValue("claude/sonnet");
  });

  it("edits the expansion pattern-criteria map of string lists", () => {
    renderSection(makeContext());

    // The map key input plus the nested per-key string-list value editor.
    expect(
      screen.getByLabelText("Pattern criteria templates key 1"),
    ).toHaveValue("backend");
    expect(
      screen.getByLabelText("Pattern criteria templates backend values item 1"),
    ).toHaveValue("{unit_tests}");
    expect(
      screen.getByLabelText("Pattern detection keywords backend values item 2"),
    ).toHaveValue("route");
  });

  it("reads checklist validation rows including the escalation-notify enum select", () => {
    renderSection(makeContext());

    const notify = screen.getByLabelText("Escalation notify method");
    expect(notify).toHaveValue("none");
    expect(within(notify).getAllByRole("option")).toHaveLength(3);
    expect(screen.getByLabelText("Max validation iterations")).toHaveValue(5);
    expect(
      screen.getByLabelText("Close-review prompt limit (characters)"),
    ).toHaveValue(32000);
  });

  it("reads workflow-engine, tmux, cron int-list, system-loop, and pipeline rows", () => {
    renderSection(makeContext());

    expect(screen.getByLabelText("Workflow timeout (seconds)")).toHaveValue(
      300,
    );
    expect(screen.getByLabelText("Scrollback history limit")).toHaveValue(
      10000,
    );
    // cron.backoff_delays is an int array rendered as a typed number list.
    expect(screen.getByLabelText("Retry backoff delay item 2")).toHaveValue(60);
    expect(
      screen.getByLabelText("Automation loop interval (seconds)"),
    ).toHaveValue(30);
    expect(screen.getByLabelText("Pipeline nesting depth limit")).toHaveValue(
      3,
    );
  });

  it("drives the live rules-enforcement toggle through the context", () => {
    const ctx = makeContext();
    renderSection(ctx);

    const toggle = screen.getByRole("switch", { name: "Enforce rules engine" });
    expect(toggle).toBeChecked();
    fireEvent.click(toggle);
    expect(ctx.setRulesEnforcement).toHaveBeenCalledWith(false);
  });

  it("mounts the workflow-variables editor", () => {
    renderSection(makeContext());
    expect(screen.getByText("variables-editor-sentinel")).toBeInTheDocument();
  });

  it("persists an edited draft row through the section Save", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    fireEvent.click(
      screen.getByRole("switch", { name: "Enable workflow engine" }),
    );
    const save = screen.getByRole("button", { name: "Save" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1));
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ "workflow.enabled": false }),
    );
  });

  it("persists the close-review prompt limit through the section Save", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    fireEvent.change(
      screen.getByLabelText("Close-review prompt limit (characters)"),
      { target: { value: "24000" } },
    );
    const save = screen.getByRole("button", { name: "Save" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1));
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        "gobby-tasks.validation.close_review_prompt_max_chars": 24000,
      }),
    );
  });

  it("degrades gracefully when the rules-enforcement surface is absent", () => {
    renderSection(makeContext({ setRulesEnforcement: undefined }));

    expect(
      screen.queryByRole("switch", { name: "Enforce rules engine" }),
    ).toBeNull();
    expect(
      screen.getByText(/Rules enforcement is unavailable/i),
    ).toBeInTheDocument();
    // Config-backed rows still render without the live rules surface.
    expect(screen.getByLabelText("Expansion capability profile")).toHaveValue(
      "feature_high",
    );
  });
});
