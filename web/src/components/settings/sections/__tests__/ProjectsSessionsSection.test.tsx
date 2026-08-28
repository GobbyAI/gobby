import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ProjectsSessionsSection } from "../ProjectsSessionsSection";
import {
  SettingsSectionContext,
  type ProjectSelectionContextValue,
  type SettingsSectionContextValue,
} from "../SettingsSectionContext";

// ProjectSelectField self-populates its options from useProjects(); stub it so
// the active-project control renders a deterministic option list. The factory
// is hoisted above imports, so the projects live inside it.
vi.mock("../../../../hooks/useProjects", () => {
  const projects = [
    { id: "project-1", name: "alpha", display_name: "Alpha workspace" },
    { id: "project-2", name: "beta", display_name: "Beta workspace" },
  ];
  return {
    useProjects: () => ({
      allProjects: projects,
      projects,
      isLoading: false,
      error: null,
    }),
  };
});

// Schema covering the rows the assertions touch: session_summary.profile
// reaches the shared FeatureProfile enum through a $ref (proving nested
// pickPaths traversal + enum resolution), mirroring the real DaemonConfig.
const SCHEMA: Record<string, unknown> = {
  $defs: {
    FeatureProfile: {
      enum: ["feature_low", "feature_mid", "feature_high"],
      type: "string",
    },
    SessionSummaryConfig: {
      type: "object",
      properties: {
        profile: { $ref: "#/$defs/FeatureProfile" },
        candidates: { type: "array", items: { type: "string" } },
        enabled: { type: "boolean" },
      },
    },
    SessionLifecycleConfig: {
      type: "object",
      properties: {
        active_session_pause_minutes: { type: "integer", minimum: 1 },
      },
    },
  },
  type: "object",
  properties: {
    session_summary: { $ref: "#/$defs/SessionSummaryConfig" },
    session_lifecycle: { $ref: "#/$defs/SessionLifecycleConfig" },
  },
};

const SUMMARY_PROMPT = "Generate a concise session summary for handoff.";

function makeConfigValues(): Record<string, unknown> {
  return {
    session_summary: {
      profile: "feature_high",
      candidates: ["claude/sonnet"],
      enabled: true,
      prompt: SUMMARY_PROMPT,
      summary_file_path: ".gobby/session_summaries",
    },
    session_lifecycle: {
      active_session_pause_minutes: 30,
      stale_session_timeout_hours: 24,
      expire_check_interval_minutes: 60,
      transcript_processing_interval_minutes: 5,
      transcript_processing_batch_size: 10,
      transcript_archive_dir: "~/.gobby/session_transcripts",
    },
    chat_history: {
      max_message_chars: 2000,
      max_total_chars: 30000,
    },
    message_tracking: {
      enabled: true,
      poll_interval: 5,
      debounce_delay: 1,
      max_message_length: 10000,
      broadcast_enabled: true,
    },
    verification_defaults: {
      unit_tests: "uv run pytest tests/ -v",
      type_check: null,
      lint: null,
      format: null,
      build: null,
      doc_tests: null,
      integration: null,
      security: null,
      code_review: null,
      custom: { smoke: "make smoke" },
    },
    validation_detection: {
      enabled: true,
      builtin_matchers_enabled: true,
      disabled_builtin_matcher_ids: [],
      recognized_wrappers: [],
      wrapper_rules: [],
      custom_matchers: [],
    },
  };
}

function makeProjectSelection(): ProjectSelectionContextValue {
  return {
    selectedProjectId: "project-1",
    onSelectProject: vi.fn(),
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
    projectSelection: makeProjectSelection(),
    ...overrides,
  };
}

function renderSection(ctx: SettingsSectionContextValue) {
  return render(
    <SettingsSectionContext.Provider value={ctx}>
      <ProjectsSessionsSection />
    </SettingsSectionContext.Provider>,
  );
}

describe("ProjectsSessionsSection", () => {
  it("wires the active-project control to the shared projectSelection context", () => {
    const ctx = makeContext();
    renderSection(ctx);

    const select = screen.getByLabelText("Active project") as HTMLSelectElement;
    expect(select).toHaveValue("project-1");
    // "No project" + the two stubbed projects.
    expect(within(select).getAllByRole("option")).toHaveLength(3);

    fireEvent.change(select, { target: { value: "project-2" } });
    expect(ctx.projectSelection?.onSelectProject).toHaveBeenCalledWith(
      "project-2",
    );
  });

  it("reads session-summary rows: enum profile, candidates, toggle, and prompt textarea", () => {
    renderSection(makeContext());

    const profile = screen.getByLabelText("Summary capability profile");
    expect(profile).toHaveValue("feature_high");
    expect(within(profile).getAllByRole("option")).toHaveLength(3);

    expect(screen.getByLabelText("Summary model candidate item 1")).toHaveValue(
      "claude/sonnet",
    );
    expect(
      screen.getByRole("switch", { name: "Generate session summaries" }),
    ).toBeChecked();
    expect(
      screen.getByLabelText("Session summary prompt template"),
    ).toHaveValue(SUMMARY_PROMPT);
  });

  it("reads session lifecycle and chat history config rows", () => {
    renderSection(makeContext());

    expect(
      screen.getByLabelText("Pause active sessions after (minutes)"),
    ).toHaveValue(30);
    expect(screen.getByLabelText("Transcript archive directory")).toHaveValue(
      "~/.gobby/session_transcripts",
    );
    expect(
      screen.queryByLabelText("Default context source"),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Max total characters")).toHaveValue(30000);
  });

  it("reads verification defaults: command fields and the custom map editor", () => {
    renderSection(makeContext());

    expect(screen.getByLabelText("Unit tests command")).toHaveValue(
      "uv run pytest tests/ -v",
    );
    // Custom map editor: one seeded entry rendered as key + value inputs.
    expect(
      screen.getByLabelText("Custom verification command key 1"),
    ).toHaveValue("smoke");
    expect(screen.getByLabelText("Value for smoke")).toHaveValue("make smoke");
  });

  it("reuses ValidationDetectionEditor for the validation_detection subtree", () => {
    renderSection(makeContext());

    const matcherConfig = screen.getByLabelText(
      "Matcher Config",
    ) as HTMLTextAreaElement;
    expect(matcherConfig.value).toContain("builtin_matchers_enabled");
    expect(matcherConfig.value).toContain("custom_matchers");
    expect(screen.getByLabelText("Preview Command")).toBeInTheDocument();
  });

  it("blocks Save for invalid validation-detection JSON and saves corrected JSON", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    const matcherConfig = screen.getByLabelText("Matcher Config");
    const save = screen.getByRole("button", { name: "Save" });

    fireEvent.change(matcherConfig, {
      target: { value: '{"enabled": false}' },
    });
    await waitFor(() => expect(save).toBeEnabled());

    fireEvent.change(matcherConfig, { target: { value: '{"enabled":' } });
    await waitFor(() => expect(save).toBeDisabled());
    fireEvent.click(save);
    expect(ctx.saveConfig).not.toHaveBeenCalled();

    const correctedJson = '{"enabled": true, "custom_matchers": []}';
    fireEvent.change(matcherConfig, { target: { value: correctedJson } });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1));
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        validation_detection: { enabled: true, custom_matchers: [] },
      }),
    );
    expect(matcherConfig).toHaveValue(correctedJson);
  });

  it("persists an edited config row through the section draft Save", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    fireEvent.click(
      screen.getByRole("switch", { name: "Broadcast message events" }),
    );

    const save = screen.getByRole("button", { name: "Save" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1));
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ "message_tracking.broadcast_enabled": false }),
    );
  });

  it("degrades gracefully when project selection is absent", () => {
    renderSection(makeContext({ projectSelection: undefined }));

    expect(screen.queryByLabelText("Active project")).toBeNull();
    expect(
      screen.getByText(/Project selection is unavailable/i),
    ).toBeInTheDocument();
    // Config-backed controls still render without the live project surface.
    expect(screen.getByLabelText("Summary capability profile")).toHaveValue(
      "feature_high",
    );
  });
});
