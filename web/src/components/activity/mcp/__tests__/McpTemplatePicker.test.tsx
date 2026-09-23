import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { McpTemplate } from "../../../../hooks/useMcp";
import { McpServerFields } from "../McpServerFields";
import { createMcpServerDraft, saveMcpServerDraft } from "../McpTabActions";

vi.mock("../../../../hooks/useProjects", () => ({
  useProjects: () => ({
    allProjects: [],
    projects: [],
    isLoading: false,
    error: null,
    isError: false,
  }),
}));

const CURRENT_PROJECT_ID = "11111111-1111-4111-8111-111111111111";

const templates: McpTemplate[] = [
  {
    name: "github",
    description: "Connect a GitHub MCP server.",
    owner: "gobby",
    scope: "global",
    params: [
      {
        name: "visibility",
        required: true,
        secret: false,
        env: "GITHUB_VISIBILITY",
        arg_flag: null,
        choices: ["public", "private"],
        description: "Repository visibility",
      },
      {
        name: "api_token",
        required: false,
        secret: true,
        env: "GITHUB_TOKEN",
        arg_flag: null,
        choices: [],
        description: "Existing secret reference",
      },
    ],
  },
];

function renderTemplateFields(
  onSave: (draft: ReturnType<typeof createMcpServerDraft>) => Promise<boolean>,
) {
  const fetchTemplates = vi.fn(async () => undefined);
  render(
    <McpServerFields
      mode="create"
      source={createMcpServerDraft({ project_id: CURRENT_PROJECT_ID })}
      currentProjectId={CURRENT_PROJECT_ID}
      templates={templates}
      templatesLoading={false}
      templatesError={null}
      fetchTemplates={fetchTemplates}
      onSave={onSave}
    />,
  );
  return fetchTemplates;
}

async function chooseTemplate(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByLabelText("From template"));
  await user.selectOptions(screen.getByLabelText("Template"), ["github"]);
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("McpTemplatePicker", () => {
  it("renders choices as a select, blocks missing required values, and keeps secret values out of the DOM", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(async () => true);
    const fetchTemplates = renderTemplateFields(onSave);

    await chooseTemplate(user);

    expect(fetchTemplates).toHaveBeenCalledTimes(1);
    expect(fetchTemplates).toHaveBeenCalledWith(CURRENT_PROJECT_ID);
    expect(screen.getByLabelText(/Repository visibility/)).toBeInstanceOf(
      HTMLSelectElement,
    );

    await user.type(
      screen.getByLabelText(/Existing secret reference/),
      "$secret:GITHUB_TOKEN",
    );

    expect(screen.queryByDisplayValue("$secret:GITHUB_TOKEN")).toBeNull();
    expect(document.body).not.toHaveTextContent("$secret:GITHUB_TOKEN");

    await user.type(screen.getByLabelText("Server name"), "gh");
    await user.click(screen.getByText("Save"));

    expect(onSave).not.toHaveBeenCalled();
    expect(
      screen.getByText("Repository visibility is required."),
    ).toBeVisible();

    await user.selectOptions(screen.getByLabelText(/Repository visibility/), [
      "public",
    ]);
    await user.click(screen.getByText("Save"));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        values: {
          visibility: "public",
          api_token: "$secret:GITHUB_TOKEN",
        },
      }),
    );
  });

  it("posts the exact template payload for project and global scope", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response(JSON.stringify({ success: true })),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderTemplateFields(async (draft) =>
      saveMcpServerDraft({ mode: "create", draft }),
    );

    await user.type(screen.getByLabelText("Server name"), "github-work");
    await chooseTemplate(user);
    await user.selectOptions(screen.getByLabelText(/Repository visibility/), [
      "private",
    ]);
    await user.click(screen.getByText("Save"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      name: "github-work",
      template: "github",
      values: { visibility: "private" },
      scope: "project",
      project_id: CURRENT_PROJECT_ID,
    });

    await user.selectOptions(screen.getByLabelText("Scope"), ["global"]);
    await user.click(screen.getByText("Save"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      name: "github-work",
      template: "github",
      values: { visibility: "private" },
      scope: "global",
      project_id: "",
    });
  });
});
