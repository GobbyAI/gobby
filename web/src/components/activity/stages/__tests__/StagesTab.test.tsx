import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ACTIVITY_PANEL_TABS } from "../../ActivityPanelTabs";
import { StagesTab } from "../../StagesTab";

vi.mock("../../../chat/artifacts/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

type StageRecord = {
  name: string;
  display_label: string;
  description: string;
  category: string;
  default_agent: string | null;
  reviewer_agent: string | null;
  reviewer_agent_selector_json: string | null;
  review_policy: string;
  dispatch_type: string | null;
  dispatch_target: string | null;
  dispatch_inputs_json: string | null;
  position_hint: number;
  requires_human: boolean;
  is_terminal: boolean;
  default_max_work_attempts: number;
  default_max_review_rounds: number;
  deleted_at: string | null;
  is_edited: boolean;
};

type ProfileRecord = {
  id: string;
  name: string;
  display_label: string;
  description: string;
  skip_stages: string[];
  isolation: "none" | "worktree" | "clone";
  unattended: boolean;
  delivery_mode: "auto" | "pull_request";
  delivery_target_repo: string | null;
  enabled: boolean;
  source: "installed" | "project";
  project_id: string | null;
  tags: string[] | null;
  deleted_at: string | null;
  state: "bundled" | "edited" | "custom" | "deleted";
};

type FetchCall = {
  url: string;
  method: string;
  body: unknown;
};

const ORIGINAL_FETCH = globalThis.fetch;

function makeStage(overrides: Partial<StageRecord> = {}): StageRecord {
  return {
    name: "implementation",
    display_label: "Implementation",
    description: "Build the approved plan",
    category: "implementation",
    default_agent: "backend-developer",
    reviewer_agent: null,
    reviewer_agent_selector_json: null,
    review_policy: "always",
    dispatch_type: "agent",
    dispatch_target: "backend-developer",
    dispatch_inputs_json: "{}",
    position_hint: 40,
    requires_human: false,
    is_terminal: false,
    default_max_work_attempts: 2,
    default_max_review_rounds: 1,
    deleted_at: null,
    is_edited: false,
    ...overrides,
  };
}

function makeProfile(overrides: Partial<ProfileRecord> = {}): ProfileRecord {
  return {
    id: "profile-1",
    name: "fast-build",
    display_label: "Fast build",
    description: "Short autonomous build",
    skip_stages: ["verification"],
    isolation: "worktree",
    unattended: true,
    delivery_mode: "auto",
    delivery_target_repo: null,
    enabled: true,
    source: "project",
    project_id: "project-1",
    tags: ["speed"],
    deleted_at: null,
    state: "custom",
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installStagesFetch() {
  const stages = [
    makeStage(),
    makeStage({
      name: "verification",
      display_label: "Verification",
      category: "verification",
      description: "Validate the result",
      position_hint: 70,
    }),
  ];
  const profiles = [makeProfile()];
  const calls: FetchCall[] = [];

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const requestUrl = new URL(url, "http://localhost");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method, body });

    if (requestUrl.pathname === "/api/stages/registry" && method === "GET") {
      const includeDeleted = requestUrl.searchParams.get("include_deleted") === "true";
      return jsonResponse({
        stages: includeDeleted ? stages : stages.filter((stage) => !stage.deleted_at),
      });
    }

    const stageMatch = requestUrl.pathname.match(/^\/api\/stages\/registry\/([^/]+)$/);
    if (stageMatch && method === "DELETE") {
      const stageName = decodeURIComponent(stageMatch[1]);
      const stage = stages.find((candidate) => candidate.name === stageName);
      if (stage) stage.deleted_at = "2026-07-14T00:00:00Z";
      return jsonResponse(stage ?? { detail: "not found" }, stage ? 200 : 404);
    }

    if (stageMatch && method === "PUT") {
      const stageName = decodeURIComponent(stageMatch[1]);
      const index = stages.findIndex((stage) => stage.name === stageName);
      if (index < 0) return jsonResponse({ detail: "not found" }, 404);
      stages[index] = { ...stages[index], ...body };
      return jsonResponse(stages[index]);
    }

    if (requestUrl.pathname === "/api/profiles" && method === "GET") {
      return jsonResponse({ profiles });
    }

    if (requestUrl.pathname === "/api/profiles" && method === "POST") {
      const created = makeProfile({
        ...body,
        id: "profile-default",
        name: body.name,
        source: body.source ?? "project",
        project_id: body.project_id ?? "project-1",
        state: "custom",
      });
      profiles.push(created);
      return jsonResponse(created, 201);
    }

    const profileMatch = requestUrl.pathname.match(/^\/api\/profiles\/([^/]+)$/);
    if (profileMatch && method === "DELETE") {
      const profileName = decodeURIComponent(profileMatch[1]);
      const profile = profiles.find((candidate) => candidate.name === profileName);
      if (profile) profile.deleted_at = "2026-07-14T00:00:00Z";
      return jsonResponse(profile ?? { detail: "not found" }, profile ? 200 : 404);
    }

    if (profileMatch && method === "PUT") {
      const profileName = decodeURIComponent(profileMatch[1]);
      const index = profiles.findIndex((profile) => profile.name === profileName);
      if (index < 0) return jsonResponse({ detail: "not found" }, 404);
      profiles[index] = { ...profiles[index], ...body };
      return jsonResponse(profiles[index]);
    }

    if (requestUrl.pathname.endsWith("/enable") && method === "POST") {
      const pathParts = requestUrl.pathname.split("/");
      const profileName = decodeURIComponent(pathParts[pathParts.length - 2] ?? "");
      const profile = profiles.find((candidate) => candidate.name === profileName);
      if (profile) profile.enabled = true;
      return jsonResponse(profile ?? { detail: "not found" }, profile ? 200 : 404);
    }

    if (requestUrl.pathname.endsWith("/disable") && method === "POST") {
      const pathParts = requestUrl.pathname.split("/");
      const profileName = decodeURIComponent(pathParts[pathParts.length - 2] ?? "");
      const profile = profiles.find((candidate) => candidate.name === profileName);
      if (profile) profile.enabled = false;
      return jsonResponse(profile ?? { detail: "not found" }, profile ? 200 : 404);
    }

    return jsonResponse({ detail: `Unhandled ${method} ${requestUrl.pathname}` }, 404);
  });

  globalThis.fetch = fetchMock as unknown as typeof fetch;
  return { calls, fetchMock, stages, profiles };
}

afterEach(() => {
  vi.restoreAllMocks();
  globalThis.fetch = ORIGINAL_FETCH;
});

describe("Stages activity tab", () => {
  it("is registered in the activity tab selector", () => {
    expect(ACTIVITY_PANEL_TABS.map((tab) => tab.id)).toContain("stages");
  });

  it("swaps the Stages and Profiles segments against live data", async () => {
    installStagesFetch();
    const user = userEvent.setup();

    render(<StagesTab projectId="project-1" />);

    expect(await screen.findByRole("button", { name: "Select Implementation" })).toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: "Profiles" }));

    expect(await screen.findByRole("button", { name: "Select Fast build" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Select Implementation" })).not.toBeInTheDocument();
  });

  it("edits stage detail through a draft with Save and Discard", async () => {
    const { calls } = installStagesFetch();
    const user = userEvent.setup();

    render(<StagesTab projectId="project-1" />);
    await user.click(await screen.findByRole("button", { name: "Select Implementation" }));

    const labelField = await screen.findByLabelText("Stage label");
    await user.clear(labelField);
    await user.type(labelField, "Build & Review");

    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.getByLabelText("Stage label")).toHaveValue("Implementation");

    await user.clear(screen.getByLabelText("Stage label"));
    await user.type(screen.getByLabelText("Stage label"), "Build Review");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(
        calls.some(
          (call) =>
            call.url.endsWith("/api/stages/registry/implementation") &&
            call.method === "PUT" &&
            (call.body as StageRecord).display_label === "Build Review",
        ),
      ).toBe(true),
    );
  });

  it("edits profile detail through a draft with Save and Discard", async () => {
    const { calls } = installStagesFetch();
    const user = userEvent.setup();

    render(<StagesTab projectId="project-1" />);
    await user.click(screen.getByRole("radio", { name: "Profiles" }));
    await user.click(await screen.findByRole("button", { name: "Select Fast build" }));

    const descriptionField = await screen.findByLabelText("Profile description");
    await user.clear(descriptionField);
    await user.type(descriptionField, "Parallel fast lane");

    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.getByLabelText("Profile description")).toHaveValue("Short autonomous build");

    await user.clear(screen.getByLabelText("Profile description"));
    await user.type(screen.getByLabelText("Profile description"), "Parallel fast lane");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(
        calls.some(
          (call) =>
            call.url.includes("/api/profiles/fast-build") &&
            call.method === "PUT" &&
            (call.body as ProfileRecord).description === "Parallel fast lane",
        ),
      ).toBe(true),
    );
  });

  it("surfaces default-profile selection as a profile row action", async () => {
    const { calls } = installStagesFetch();
    const user = userEvent.setup();

    render(<StagesTab projectId="project-1" />);
    await user.click(screen.getByRole("radio", { name: "Profiles" }));

    const row = await screen.findByRole("listitem", { name: /Fast build profile/i });
    await user.click(within(row).getByRole("button", { name: "Open actions for Fast build" }));
    await user.click(await screen.findByRole("menuitem", { name: "Set as default" }));

    await waitFor(() =>
      expect(
        calls.some(
          (call) =>
            call.url.endsWith("/api/profiles") &&
            call.method === "POST" &&
            (call.body as ProfileRecord).name === "default",
        ),
      ).toBe(true),
    );
  });

  it("requires confirmation before deleting stages and profiles", async () => {
    const { calls } = installStagesFetch();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();

    render(<StagesTab projectId="project-1" />);

    const stageRow = await screen.findByRole("listitem", { name: /Implementation stage/i });
    await user.click(
      within(stageRow).getByRole("button", { name: "Open actions for Implementation" }),
    );
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));

    expect(confirm).toHaveBeenCalledWith('Delete "Implementation"?');
    expect(calls.some((call) => call.method === "DELETE")).toBe(false);

    confirm.mockReturnValue(true);
    await user.click(
      within(stageRow).getByRole("button", { name: "Open actions for Implementation" }),
    );
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));

    await waitFor(() =>
      expect(
        calls.some(
          (call) =>
            call.url.endsWith("/api/stages/registry/implementation") &&
            call.method === "DELETE",
        ),
      ).toBe(true),
    );

    await user.click(screen.getByRole("radio", { name: "Profiles" }));
    const profileRow = await screen.findByRole("listitem", { name: /Fast build profile/i });
    await user.click(
      within(profileRow).getByRole("button", { name: "Open actions for Fast build" }),
    );
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));

    expect(confirm).toHaveBeenCalledWith('Delete "Fast build"?');
    await waitFor(() =>
      expect(
        calls.some(
          (call) => call.url.includes("/api/profiles/fast") && call.method === "DELETE",
        ),
      ).toBe(true),
    );
  });
});
