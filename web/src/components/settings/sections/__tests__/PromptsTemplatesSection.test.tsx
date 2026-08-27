import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import { PromptsTemplatesSection } from "../PromptsTemplatesSection";
import {
  SettingsSectionContext,
  type SettingsSectionContextValue,
} from "../SettingsSectionContext";
import type {
  PromptDetail,
  PromptInfo,
} from "../../../../hooks/useConfiguration";

// CodeMirror needs a real DOM editor view that jsdom cannot drive, so the
// shared editor is replaced with a labelled <textarea>. Keying the textarea on
// `ariaLabel` lets the prompt-override editor and the template editor be
// addressed independently in tests.
vi.mock("../../../shared/CodeMirrorEditor", () => ({
  CodeMirrorEditor: ({
    content,
    ariaLabel,
    onChange,
  }: {
    content: string;
    ariaLabel?: string;
    onChange?: (content: string) => void;
  }) => (
    <textarea
      aria-label={ariaLabel}
      value={content}
      onChange={(event) => onChange?.(event.target.value)}
    />
  ),
}));

const SCHEMA: Record<string, unknown> = { type: "object", properties: {} };

function makePrompts(): PromptInfo[] {
  return [
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
  ];
}

function makeDetail(path: string): PromptDetail {
  if (path === "tasks/expand") {
    return {
      path,
      description: "Expand a task into subtasks",
      content: "Overridden expand body",
      source: "overridden",
      has_override: true,
      bundled_content: "Bundled expand body",
      variables: {},
    };
  }
  return {
    path,
    description: "Summarize an agent run",
    content: "Bundled summary body",
    source: "bundled",
    has_override: false,
    bundled_content: null,
    variables: {},
  };
}

function makeContext(
  overrides: Partial<SettingsSectionContextValue> = {},
): SettingsSectionContextValue {
  return {
    schema: SCHEMA,
    configValues: {},
    secretKeys: [],
    isLoading: false,
    saveConfig: vi.fn(async () => ({ ok: true })),
    registerDirtyGuard: () => () => {},
    prompts: makePrompts(),
    promptCategories: { agents: 1, tasks: 1 },
    getPromptDetail: vi.fn(async (path: string) => makeDetail(path)),
    savePromptOverride: vi.fn(async () => true),
    deletePromptOverride: vi.fn(async () => true),
    templateContent: "defaults:\n  enabled: true\n",
    saveTemplate: vi.fn(async () => ({ ok: true })),
    exportConfig: vi.fn(async () => ({
      revision: 7,
      content: "memory:\n  enabled: true\n",
    })),
    importConfig: vi.fn(async () => ({
      success: true,
      summary: "Imported 3 keys",
    })),
    ...overrides,
  };
}

function renderSection(ctx: SettingsSectionContextValue) {
  return render(
    <SettingsSectionContext.Provider value={ctx}>
      <PromptsTemplatesSection />
    </SettingsSectionContext.Provider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("PromptsTemplatesSection", () => {
  it("renders the three section surfaces", () => {
    renderSection(makeContext());

    expect(screen.getByText("Prompt overrides")).toBeInTheDocument();
    expect(screen.getByText("Full configuration template")).toBeInTheDocument();
    expect(screen.getByText("Backup & restore")).toBeInTheDocument();
  });

  it("lists prompts with a source label and no editor open", () => {
    renderSection(makeContext());

    expect(
      screen.getByRole("button", { name: "Edit prompt agents/summary" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Edit prompt tasks/expand" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Bundled")).toBeInTheDocument();
    expect(screen.getByText("Overridden")).toBeInTheDocument();
  });

  it("filters the prompt list by category", () => {
    renderSection(makeContext());

    fireEvent.change(
      screen.getByRole("combobox", { name: "Filter prompts by category" }),
      { target: { value: "tasks" } },
    );

    expect(
      screen.queryByRole("button", { name: "Edit prompt agents/summary" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Edit prompt tasks/expand" }),
    ).toBeInTheDocument();
  });

  it("opens the override editor with the fetched prompt content", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    fireEvent.click(
      screen.getByRole("button", { name: "Edit prompt agents/summary" }),
    );

    await waitFor(() =>
      expect(ctx.getPromptDetail).toHaveBeenCalledWith("agents/summary"),
    );
    expect(screen.getByLabelText("Prompt override content")).toHaveValue(
      "Bundled summary body",
    );
  });

  it("keeps the latest prompt when detail responses arrive out of order", async () => {
    const resolvers = new Map<string, (detail: PromptDetail) => void>();
    const getPromptDetail = vi.fn(
      (path: string) =>
        new Promise<PromptDetail>((resolve) => {
          resolvers.set(path, resolve);
        }),
    );
    renderSection(makeContext({ getPromptDetail }));

    fireEvent.click(
      screen.getByRole("button", { name: "Edit prompt agents/summary" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Edit prompt tasks/expand" }),
    );

    await act(async () => {
      resolvers.get("tasks/expand")?.(makeDetail("tasks/expand"));
    });
    expect(screen.getByLabelText("Prompt override content")).toHaveValue(
      "Overridden expand body",
    );

    await act(async () => {
      resolvers.get("agents/summary")?.(makeDetail("agents/summary"));
    });
    expect(screen.getByLabelText("Prompt override content")).toHaveValue(
      "Overridden expand body",
    );
  });

  it("shows prompt load, save, and revert failures", async () => {
    const getPromptDetail = vi
      .fn<(path: string) => Promise<PromptDetail | null>>()
      .mockRejectedValueOnce(new Error("load failed"))
      .mockResolvedValueOnce(makeDetail("tasks/expand"));
    const ctx = makeContext({
      getPromptDetail,
      savePromptOverride: vi.fn(async () => false),
      deletePromptOverride: vi.fn(async () => false),
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderSection(ctx);

    fireEvent.click(
      screen.getByRole("button", { name: "Edit prompt agents/summary" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load this prompt.",
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Edit prompt tasks/expand" }),
    );
    const editor = await screen.findByLabelText("Prompt override content");
    fireEvent.change(editor, { target: { value: "Changed content" } });
    fireEvent.click(screen.getByRole("button", { name: "Save override" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not save the override.",
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Revert to bundled default" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not revert the override.",
    );
  });

  it("saves an edited override through onSaveOverride", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    fireEvent.click(
      screen.getByRole("button", { name: "Edit prompt agents/summary" }),
    );
    const editor = await screen.findByLabelText("Prompt override content");
    fireEvent.change(editor, { target: { value: "My custom summary" } });
    fireEvent.click(screen.getByRole("button", { name: "Save override" }));

    await waitFor(() =>
      expect(ctx.savePromptOverride).toHaveBeenCalledWith(
        "agents/summary",
        "My custom summary",
      ),
    );
  });

  it("reverts an overridden prompt after confirmation", async () => {
    const ctx = makeContext();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderSection(ctx);

    fireEvent.click(
      screen.getByRole("button", { name: "Edit prompt tasks/expand" }),
    );
    const revert = await screen.findByRole("button", {
      name: "Revert to bundled default",
    });
    fireEvent.click(revert);

    expect(confirmSpy).toHaveBeenCalled();
    await waitFor(() =>
      expect(ctx.deletePromptOverride).toHaveBeenCalledWith("tasks/expand"),
    );
    expect(screen.getByLabelText("Prompt override content")).toHaveValue(
      "Bundled expand body",
    );
  });

  it("does not offer Revert for a prompt with no override", async () => {
    renderSection(makeContext());

    fireEvent.click(
      screen.getByRole("button", { name: "Edit prompt agents/summary" }),
    );
    await screen.findByLabelText("Prompt override content");

    expect(
      screen.queryByRole("button", { name: "Revert to bundled default" }),
    ).not.toBeInTheDocument();
  });

  it("returns to the prompt list from the editor", async () => {
    renderSection(makeContext());

    fireEvent.click(
      screen.getByRole("button", { name: "Edit prompt agents/summary" }),
    );
    await screen.findByLabelText("Prompt override content");
    fireEvent.click(
      screen.getByRole("button", { name: "Back to prompt list" }),
    );

    expect(
      screen.getByRole("button", { name: "Edit prompt agents/summary" }),
    ).toBeInTheDocument();
  });

  it("saves the full template and surfaces the restart banner", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    const editor = screen.getByLabelText("Configuration template");
    expect(editor).toHaveValue("defaults:\n  enabled: true\n");
    fireEvent.change(editor, {
      target: { value: "defaults:\n  enabled: false\n" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save template" }));

    await waitFor(() =>
      expect(ctx.saveTemplate).toHaveBeenCalledWith(
        "defaults:\n  enabled: false\n",
      ),
    );
    expect(
      await screen.findByRole("button", { name: "Restart now" }),
    ).toBeInTheDocument();
  });

  it("surfaces protected restart details and offers a force retry", async () => {
    const protectedRun = {
      run_id: "run-1",
      job_id: "job-1",
      job_name: "gobby:memory-dream",
      started_at: "2026-08-27T07:00:00+00:00",
      elapsed_seconds: 3725,
      remaining_seconds: 12475,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: "restart_protected",
            message: "Restart blocked by active protected cron runs",
            protected_runs: [protectedRun],
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ status: "restarting" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const ctx = makeContext();
    renderSection(ctx);

    fireEvent.change(screen.getByLabelText("Configuration template"), {
      target: { value: "defaults:\n  enabled: false\n" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save template" }));
    fireEvent.click(await screen.findByRole("button", { name: "Restart now" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "gobby:memory-dream (running 1h 2m 5s, at most 3h 27m 55s left)",
    );
    const forceButton = screen.getByRole("button", {
      name: "Force restart",
    });
    expect(forceButton).toBeInTheDocument();

    fireEvent.click(forceButton);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/admin/restart");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/admin/restart?force=true");
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Force restart" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("exports the configuration bundle", async () => {
    const ctx = makeContext();
    const createUrl = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:mock");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    renderSection(ctx);

    fireEvent.click(
      screen.getByRole("button", { name: "Export configuration" }),
    );

    await waitFor(() => expect(ctx.exportConfig).toHaveBeenCalledTimes(1));
    expect(createUrl).toHaveBeenCalled();
  });

  it("imports a YAML configuration document from a file and shows the result", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    const content = "memory:\n  enabled: true\n";
    const file = new File([content], "config.yaml", {
      type: "application/yaml",
    });
    fireEvent.change(screen.getByLabelText("Import configuration file"), {
      target: { files: [file] },
    });

    await waitFor(() => expect(ctx.importConfig).toHaveBeenCalledWith(content));
    expect(await screen.findByText(/Imported 3 keys/)).toBeInTheDocument();
  });

  it("reports a section dirty guard once the template is edited", () => {
    const guards: Array<() => boolean> = [];
    const ctx = makeContext({
      registerDirtyGuard: (_section, isDirty) => {
        guards.push(isDirty);
        return () => {};
      },
    });
    renderSection(ctx);

    expect(guards.some((isDirty) => isDirty())).toBe(false);

    fireEvent.change(screen.getByLabelText("Configuration template"), {
      target: { value: "defaults:\n  enabled: false\n" },
    });

    expect(guards.some((isDirty) => isDirty())).toBe(true);
  });

  it("degrades gracefully when the standalone surfaces are unavailable", () => {
    renderSection(
      makeContext({
        getPromptDetail: undefined,
        savePromptOverride: undefined,
        deletePromptOverride: undefined,
        saveTemplate: undefined,
        exportConfig: undefined,
        importConfig: undefined,
      }),
    );

    expect(
      screen.getByText("Prompt editing is unavailable."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("The template editor is unavailable."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Backup and restore is unavailable."),
    ).toBeInTheDocument();
  });
});
