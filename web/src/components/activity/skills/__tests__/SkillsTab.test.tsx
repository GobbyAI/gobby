import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ACTIVITY_PANEL_TABS } from "../../ActivityPanelTabs";
import { SkillsTab } from "../../SkillsTab";
import { renderWithActivityActions as render } from "../../../../test/helpers";
import {
  createMockFetch,
  type MockFetchInstance,
} from "../../../../test/mocks/fetch";

vi.mock("../../../../hooks/useWebSocketEvent", () => ({
  useWebSocketEvent: vi.fn(),
}));

// Capture the confirm options so tests can assert the permanent-delete warning
// copy without driving the Radix dialog internals.
const confirmMock = vi.hoisted(() => vi.fn(async () => true));
vi.mock("../../../../hooks/useConfirmDialog", () => ({
  useConfirmDialog: () => ({
    confirm: confirmMock,
    ConfirmDialogElement: null,
  }),
}));

vi.mock("../../../shared/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

vi.mock("../../../shared/MarkdownBody", async (importOriginal) => ({
  markdownBodyClassName: (
    await importOriginal<typeof import("../../../shared/MarkdownBody")>()
  ).markdownBodyClassName,
  MarkdownBody: ({ content }: { content: string }) => (
    <div data-testid="markdown-body">{content}</div>
  ),
}));

vi.mock("../../../shared/CodeMirrorEditor", () => ({
  CodeMirrorEditor: ({
    content,
    onChange,
    readOnly,
    ariaLabel,
  }: {
    content: string;
    onChange?: (content: string) => void;
    readOnly?: boolean;
    ariaLabel?: string;
  }) => (
    <textarea
      aria-label={ariaLabel ?? "Skill content markdown"}
      readOnly={readOnly}
      value={content}
      onChange={(event) => onChange?.(event.target.value)}
    />
  ),
}));

type SkillRecord = {
  id: string;
  name: string;
  description: string;
  content: string;
  version: string | null;
  license: string | null;
  compatibility: string | null;
  allowed_tools: string[] | null;
  metadata: Record<string, unknown> | null;
  source_path: string | null;
  source_type: string | null;
  source_ref: string | null;
  source: string | null;
  hub_name: string | null;
  hub_slug: string | null;
  hub_version: string | null;
  enabled: boolean;
  always_apply: boolean;
  injection_format: string;
  project_id: string | null;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
};

let mockFetch: MockFetchInstance;

function makeSkill(overrides: Partial<SkillRecord>): SkillRecord {
  return {
    id: "skill-default",
    name: "Default skill",
    description: "Default description",
    content: "# Default\n",
    version: "1.0.0",
    license: "MIT",
    compatibility: "gobby >=0.5",
    allowed_tools: ["shell"],
    metadata: { category: "General" },
    source_path: null,
    source_type: null,
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
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    ...overrides,
  };
}

function setupFetch(skills: SkillRecord[]) {
  mockFetch = createMockFetch();
  mockFetch.mockJsonResponse("/api/projects", [
    {
      id: "project-1",
      name: "gobby",
      display_name: "Gobby",
      checkout: { machine_id: "machine-1", root_path: "/repo" },
      github_repo: null,
      session_count: 0,
      open_task_count: 0,
    },
  ]);
  mockFetch.mockJsonResponse(/\/api\/skills\?/, { skills });
  mockFetch.mockJsonResponse(/\/api\/skills\/[^/]+\/export$/, {
    filename: "skill.md",
    content: "# Exported",
  });
  mockFetch.mockJsonResponse(/\/api\/skills\/[^/]+\/move-to-project\?/, {
    skill: skills[0],
  });
  mockFetch.mockJsonResponse(/\/api\/skills\/[^/]+\/move-to-installed$/, {
    skill: skills[0],
  });
  mockFetch.mockJsonResponse(/\/api\/skills\/[^/]+\/restore$/, {
    restored: true,
    skill: { ...skills[0], deleted_at: null },
  });
  mockFetch.mockJsonResponse(/\/api\/skills\/[^/]+$/, {
    ...skills[0],
    enabled: false,
  });
}

function lastJsonBodyFor(pathPart: string) {
  const call = mockFetch.fn.mock.calls
    .slice()
    .reverse()
    .find(([url]) => String(url).includes(pathPart));
  const init = call?.[1] as RequestInit | undefined;
  return init?.body ? JSON.parse(String(init.body)) : null;
}

function callWithMethod(pathPart: string, method: string) {
  return mockFetch.fn.mock.calls.find(
    ([url, init]) =>
      String(url).includes(pathPart) &&
      (init as RequestInit | undefined)?.method === method,
  );
}

describe("Skills activity Installed segment", () => {
  beforeEach(() => {
    window.localStorage.removeItem("gobby-skills-segment-v1");
    confirmMock.mockReset();
    confirmMock.mockResolvedValue(true);
  });

  afterEach(() => {
    mockFetch?.restore();
    vi.restoreAllMocks();
    window.localStorage.removeItem("gobby-skills-segment-v1");
  });

  it("registers the Skills tab and filters installed skills in the toolbar", async () => {
    setupFetch([
      makeSkill({
        id: "sk-installed",
        name: "Code navigator",
        description: "Inspect indexed code",
        metadata: { category: "Navigation" },
        source: "installed",
      }),
      makeSkill({
        id: "sk-project",
        name: "Bridge pack",
        description: "Project scoped UI bridge",
        metadata: { category: "Automation" },
        source: "project",
        project_id: "project-1",
        enabled: false,
      }),
      makeSkill({
        id: "sk-hub",
        name: "Hub curator",
        description: "Installed from the shared hub",
        metadata: { category: "Automation" },
        source: "installed",
        hub_name: "community",
      }),
    ]);

    const user = userEvent.setup();

    expect(ACTIVITY_PANEL_TABS.some((tab) => tab.id === "skills")).toBe(true);
    render(<SkillsTab projectId="project-1" />);

    expect(
      await screen.findByRole("button", { name: "Select Code navigator" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Installed" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.queryByText("INSTALLED")).not.toBeInTheDocument();
    expect(screen.getByText("PROJECT")).toBeInTheDocument();
    expect(screen.getByText("hub")).toBeInTheDocument();

    const hubSkill = screen.getByRole("button", { name: "Select Hub curator" });
    hubSkill.focus();
    await user.keyboard("{Enter}");
    expect(hubSkill.parentElement).toHaveClass("activity-list-row--selected");

    // Source/category filters live behind the header Filter trigger now.
    await user.click(screen.getByRole("button", { name: "Filter skills" }));
    await user.selectOptions(screen.getByLabelText("Skill source"), "project");
    expect(screen.getAllByText("Bridge pack").length).toBeGreaterThan(0);
    expect(screen.queryByText("Code navigator")).not.toBeInTheDocument();

    await user.selectOptions(
      screen.getByLabelText("Skill category"),
      "Automation",
    );
    // The search bar is hidden until the header Search toggle opens it.
    await user.click(screen.getByRole("button", { name: "Search skills" }));
    await user.type(
      screen.getByRole("searchbox", { name: "Search skills" }),
      "bridge",
    );

    expect(screen.getAllByText("Bridge pack").length).toBeGreaterThan(0);
    expect(screen.queryByText("Hub curator")).not.toBeInTheDocument();
  });

  it("exposes row actions through the shared kebab menu", async () => {
    setupFetch([
      makeSkill({
        id: "sk-installed",
        name: "Code navigator",
        source: "installed",
      }),
      makeSkill({
        id: "sk-project",
        name: "Bridge pack",
        source: "project",
        project_id: "project-1",
        enabled: false,
      }),
    ]);

    const user = userEvent.setup();
    render(<SkillsTab projectId="project-1" />);

    expect(
      await screen.findByRole("button", { name: "Select Code navigator" }),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Open actions for Code navigator" }),
    );

    const installedMenu = screen.getByRole("menu", {
      name: "Actions for Code navigator",
    });
    expect(
      within(installedMenu).getByRole("menuitem", { name: "Disable" }),
    ).toBeInTheDocument();
    expect(
      within(installedMenu).getByRole("menuitem", { name: "Move to project" }),
    ).toBeInTheDocument();
    expect(
      within(installedMenu).getByRole("menuitem", { name: "Export" }),
    ).toBeInTheDocument();
    expect(
      within(installedMenu).getByRole("menuitem", { name: "Delete" }),
    ).toBeInTheDocument();

    await user.click(
      within(installedMenu).getByRole("menuitem", { name: "Disable" }),
    );
    expect(lastJsonBodyFor("/api/skills/sk-installed")).toEqual({
      enabled: false,
    });

    await user.click(
      screen.getByRole("button", { name: "Open actions for Bridge pack" }),
    );
    const projectMenu = screen.getByRole("menu", {
      name: "Actions for Bridge pack",
    });
    expect(
      within(projectMenu).getByRole("menuitem", { name: "Move to installed" }),
    ).toBeInTheDocument();
  });

  it("limits deleted skill menus to Restore, Export, and Delete forever (#19162)", async () => {
    setupFetch([
      makeSkill({ id: "sk-live", name: "Live skill" }),
      makeSkill({
        id: "sk-gone",
        name: "Old skill",
        deleted_at: "2026-07-01T00:00:00Z",
      }),
    ]);

    const user = userEvent.setup();
    render(<SkillsTab projectId="project-1" />);

    await screen.findByRole("button", { name: "Select Live skill" });
    expect(screen.queryByText("Old skill")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Filter skills" }));
    await user.selectOptions(screen.getByLabelText("Skill source"), "deleted");

    await user.click(
      screen.getByRole("button", { name: "Open actions for Old skill" }),
    );
    const menu = screen.getByRole("menu", { name: "Actions for Old skill" });
    expect(
      within(menu).getByRole("menuitem", { name: "Restore" }),
    ).toBeInTheDocument();
    expect(
      within(menu).getByRole("menuitem", { name: "Export" }),
    ).toBeInTheDocument();
    expect(
      within(menu).getByRole("menuitem", { name: "Delete forever" }),
    ).toBeInTheDocument();
    expect(
      within(menu).queryByRole("menuitem", { name: "Disable" }),
    ).toBeNull();
    expect(
      within(menu).queryByRole("menuitem", { name: /Move to/ }),
    ).toBeNull();
  });

  it("purges a soft-deleted skill after a permanent-delete confirm (#19162)", async () => {
    setupFetch([
      makeSkill({
        id: "sk-gone",
        name: "Old skill",
        deleted_at: "2026-07-01T00:00:00Z",
      }),
    ]);

    const user = userEvent.setup();
    render(<SkillsTab projectId="project-1" />);

    await user.click(
      await screen.findByRole("button", { name: "Filter skills" }),
    );
    await user.selectOptions(screen.getByLabelText("Skill source"), "deleted");
    await user.click(
      screen.getByRole("button", { name: "Open actions for Old skill" }),
    );
    await user.click(screen.getByRole("menuitem", { name: "Delete forever" }));

    await waitFor(() => {
      expect(confirmMock).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Permanently delete Old skill?",
          description: expect.stringContaining("cannot be restored"),
          confirmLabel: "Delete forever",
          destructive: true,
        }),
      );
    });
    await waitFor(() => {
      expect(callWithMethod("/api/skills/sk-gone", "DELETE")).toBeTruthy();
    });
  });

  it("keeps the skill when the delete confirm is cancelled (#19162)", async () => {
    confirmMock.mockResolvedValue(false);
    setupFetch([makeSkill({ id: "sk-live", name: "Live skill" })]);

    const user = userEvent.setup();
    render(<SkillsTab projectId="project-1" />);

    await screen.findByRole("button", { name: "Select Live skill" });
    await user.click(
      screen.getByRole("button", { name: "Open actions for Live skill" }),
    );
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));

    await waitFor(() => {
      expect(confirmMock).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Delete Live skill?",
          description: expect.stringContaining("restored"),
        }),
      );
    });
    expect(callWithMethod("/api/skills/sk-live", "DELETE")).toBeUndefined();
  });

  it("restores a soft-deleted skill from the row menu (#19162)", async () => {
    setupFetch([
      makeSkill({
        id: "sk-gone",
        name: "Old skill",
        deleted_at: "2026-07-01T00:00:00Z",
      }),
    ]);

    const user = userEvent.setup();
    render(<SkillsTab projectId="project-1" />);

    await user.click(
      await screen.findByRole("button", { name: "Filter skills" }),
    );
    await user.selectOptions(screen.getByLabelText("Skill source"), "deleted");
    await user.click(
      screen.getByRole("button", { name: "Open actions for Old skill" }),
    );
    await user.click(screen.getByRole("menuitem", { name: "Restore" }));

    await waitFor(() => {
      expect(
        callWithMethod("/api/skills/sk-gone/restore", "POST"),
      ).toBeTruthy();
    });
    expect(confirmMock).not.toHaveBeenCalled();
  });

  it("saves and discards draft field edits from installed detail", async () => {
    setupFetch([
      makeSkill({
        id: "sk-installed",
        name: "Code navigator",
        description: "Inspect indexed code",
        version: "1.2.3",
        license: "Apache-2.0",
        compatibility: "gobby >=0.5",
        allowed_tools: ["shell", "gcode"],
      }),
    ]);

    const user = userEvent.setup();
    render(<SkillsTab projectId="project-1" />);

    await user.click(
      await screen.findByRole("button", { name: /Select Code navigator/i }),
    );
    const descriptionField = await screen.findByLabelText("Skill description");

    await user.clear(descriptionField);
    await user.type(descriptionField, "Updated indexed-code guidance");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(lastJsonBodyFor("/api/skills/sk-installed")).toEqual(
        expect.objectContaining({
          description: "Updated indexed-code guidance",
        }),
      );
    });

    await user.clear(screen.getByLabelText("Skill version"));
    await user.type(screen.getByLabelText("Skill version"), "9.9.9");
    expect(screen.getByDisplayValue("9.9.9")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.getByDisplayValue("1.2.3")).toBeInTheDocument();
  });

  it("edits skill markdown from the Content alternate view behind an explicit Edit", async () => {
    setupFetch([
      makeSkill({
        id: "sk-installed",
        name: "Code navigator",
        content: "# Code navigator\nUse gcode first.\n",
      }),
    ]);

    const user = userEvent.setup();
    render(<SkillsTab projectId="project-1" />);

    await user.click(
      await screen.findByRole("button", { name: /Select Code navigator/i }),
    );
    await user.click(await screen.findByRole("button", { name: "Content" }));

    // Read-only by default: rendered markdown, no editor until Edit is clicked.
    expect(await screen.findByTestId("markdown-body")).toHaveTextContent(
      "Use gcode first.",
    );
    expect(
      screen.queryByLabelText("Skill content markdown"),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Edit" }));
    const editor = await screen.findByLabelText("Skill content markdown");
    expect(editor).toHaveValue("# Code navigator\nUse gcode first.\n");

    fireEvent.change(editor, {
      target: { value: "# Code navigator\nUse gcode before raw grep.\n" },
    });
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(lastJsonBodyFor("/api/skills/sk-installed")).toEqual(
        expect.objectContaining({
          content: "# Code navigator\nUse gcode before raw grep.\n",
        }),
      );
    });

    // Saving exits edit mode back to the read view.
    expect(
      screen.queryByLabelText("Skill content markdown"),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.getByLabelText("Skill description")).toBeInTheDocument();
  });

  it("cancel abandons content edits without saving", async () => {
    setupFetch([
      makeSkill({
        id: "sk-installed",
        name: "Code navigator",
        content: "# Code navigator\nUse gcode first.\n",
      }),
    ]);

    const user = userEvent.setup();
    render(<SkillsTab projectId="project-1" />);

    await user.click(
      await screen.findByRole("button", { name: /Select Code navigator/i }),
    );
    await user.click(await screen.findByRole("button", { name: "Content" }));
    await user.click(await screen.findByRole("button", { name: "Edit" }));

    fireEvent.change(screen.getByLabelText("Skill content markdown"), {
      target: { value: "# Abandoned\n" },
    });
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(
      screen.queryByLabelText("Skill content markdown"),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("markdown-body")).toHaveTextContent(
      "Use gcode first.",
    );
    expect(
      mockFetch.fn.mock.calls.some(
        ([, init]) => (init as RequestInit | undefined)?.method === "PUT",
      ),
    ).toBe(false);
  });

  it("lists reference files and edits one through the read-only-first flow", async () => {
    setupFetch([
      makeSkill({
        id: "sk-installed",
        name: "Code navigator",
        content: "# Code navigator\n",
      }),
    ]);
    mockFetch.mockJsonResponse(/\/api\/skills\/sk-installed\/files$/, {
      files: [
        {
          path: "references/usage.md",
          file_type: "reference",
          size_bytes: 20,
          content_hash: "abc",
        },
      ],
    });
    mockFetch.mockJsonResponse(
      /\/api\/skills\/sk-installed\/files\/references\/usage\.md$/,
      {
        path: "references/usage.md",
        file_type: "reference",
        size_bytes: 20,
        content_hash: "abc",
        content: "# Usage\nOriginal reference.\n",
      },
    );

    const user = userEvent.setup();
    render(<SkillsTab projectId="project-1" />);

    await user.click(
      await screen.findByRole("button", { name: /Select Code navigator/i }),
    );
    await user.click(await screen.findByRole("button", { name: "Content" }));

    const filesNav = await screen.findByRole("navigation", {
      name: "Skill files",
    });
    expect(
      within(filesNav).getByRole("button", { name: "SKILL.md" }),
    ).toBeInTheDocument();

    await user.click(
      within(filesNav).getByRole("button", { name: "references/usage.md" }),
    );
    expect(await screen.findByTestId("markdown-body")).toHaveTextContent(
      "Original reference.",
    );

    await user.click(screen.getByRole("button", { name: "Edit" }));
    const editor = await screen.findByLabelText("references/usage.md content");
    expect(editor).toHaveValue("# Usage\nOriginal reference.\n");

    fireEvent.change(editor, {
      target: { value: "# Usage\nUpdated reference.\n" },
    });
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(
        lastJsonBodyFor("/api/skills/sk-installed/files/references/usage.md"),
      ).toEqual({ content: "# Usage\nUpdated reference.\n" });
    });
    expect(
      screen.queryByLabelText("references/usage.md content"),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("markdown-body")).toHaveTextContent(
      "Updated reference.",
    );
  });

  it("keeps the skill editor draft open and shows update failures", async () => {
    setupFetch([
      makeSkill({
        id: "sk-installed",
        name: "Code navigator",
        content: "# Code navigator\nUse gcode first.\n",
      }),
    ]);

    const user = userEvent.setup();
    render(<SkillsTab projectId="project-1" />);

    await user.click(
      await screen.findByRole("button", { name: /Select Code navigator/i }),
    );
    await user.click(screen.getByRole("button", { name: "Content" }));
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Skill content markdown"), {
      target: { value: "# Code navigator\nKeep this draft.\n" },
    });

    mockFetch.resetRoutes();
    mockFetch.mockJsonResponse(
      /\/api\/skills\/sk-installed$/,
      { detail: "Skill update was rejected" },
      { status: 409 },
    );
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText("Skill update was rejected"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Skill content markdown")).toHaveValue(
      "# Code navigator\nKeep this draft.\n",
    );
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
  });

  it("shows export failures", async () => {
    setupFetch([makeSkill({ id: "sk-installed", name: "Code navigator" })]);

    const user = userEvent.setup();
    render(<SkillsTab projectId="project-1" />);

    await screen.findByRole("button", { name: /Select Code navigator/i });
    await user.click(
      screen.getByRole("button", { name: "Open actions for Code navigator" }),
    );
    mockFetch.resetRoutes();
    mockFetch.mockJsonResponse(
      /\/api\/skills\/sk-installed\/export$/,
      { detail: "Skill export is unavailable" },
      { status: 503 },
    );
    await user.click(screen.getByRole("menuitem", { name: "Export" }));

    expect(
      await screen.findByText("Skill export is unavailable"),
    ).toBeInTheDocument();
  });
});
