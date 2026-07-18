import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SkillsTab } from "../../SkillsTab";
import { createMockFetch, type MockFetchInstance } from "../../../../test/mocks/fetch";

vi.mock("../../../../hooks/useWebSocketEvent", () => ({
  useWebSocketEvent: vi.fn(),
}));

vi.mock("../../../shared/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

type HubResult = {
  slug: string;
  display_name: string;
  description: string;
  hub_name: string;
  version: string | null;
  score: number | null;
  license?: string | null;
  content?: string | null;
};

type InstalledSkill = {
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

function makeInstalledSkill(overrides: Partial<InstalledSkill>): InstalledSkill {
  return {
    id: "skill-hub-installed",
    name: "Review Sentinel",
    description: "Review pull requests before merge.",
    content: "# Review Sentinel\n\nCheck patches carefully.",
    version: "1.2.0",
    license: "Apache-2.0",
    compatibility: null,
    allowed_tools: [],
    metadata: { category: "Review" },
    source_path: "hub:clawdhub/review-sentinel",
    source_type: "hub",
    source_ref: null,
    source: "installed",
    hub_name: "clawdhub",
    hub_slug: "review-sentinel",
    hub_version: "1.2.0",
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

function setupHubFetch({
  results,
  scanResult,
  installedSkill,
}: {
  results: HubResult[];
  scanResult: Record<string, unknown>;
  installedSkill?: InstalledSkill;
}) {
  mockFetch = createMockFetch();
  mockFetch.mockJsonResponse(/\/api\/skills\?/, { skills: [] });
  mockFetch.mockJsonResponse(/\/api\/skills\/hubs$/, {
    hubs: [
      {
        name: "clawdhub",
        type: "clawdhub",
        base_url: null,
        repo: null,
        auth_required: false,
        auth_configured: true,
      },
      {
        name: "skillsmp",
        type: "skillsmp",
        base_url: "https://skillsmp.com/api/v1",
        repo: null,
        auth_required: true,
        auth_key_name: "SKILLSMP_API_KEY",
        auth_configured: false,
      },
    ],
  });
  mockFetch.mockJsonResponse(/\/api\/skills\/hubs\/search\?/, {
    query: "review",
    results,
    count: results.length,
    hub_errors: { skillsmp: "SKILLSMP_API_KEY missing" },
  });
  mockFetch.mockJsonResponse("/api/skills/scan", scanResult);
  mockFetch.mockJsonResponse("/api/skills/hubs/install", {
    installed: true,
    skill: installedSkill ?? makeInstalledSkill({}),
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

async function searchHub(user: ReturnType<typeof userEvent.setup>) {
  render(<SkillsTab projectId="project-1" />);

  await user.click(screen.getByRole("radio", { name: "Hub" }));
  await screen.findByRole("combobox", { name: "Hub source" });

  await user.selectOptions(screen.getByRole("combobox", { name: "Hub source" }), "clawdhub");
  await user.type(screen.getByRole("searchbox", { name: "Search hub skills" }), "review");
  await user.click(screen.getByRole("button", { name: "Search hub skills" }));

  const result = await screen.findByRole("button", { name: "Select Review Sentinel" });
  await user.click(result);
}

describe("Skills activity Hub segment", () => {
  beforeEach(() => {
    window.localStorage.removeItem("gobby-skills-segment-v1");
  });

  afterEach(() => {
    mockFetch?.restore();
    vi.restoreAllMocks();
    window.localStorage.removeItem("gobby-skills-segment-v1");
  });

  it("searches configured hubs and gates unsafe installs on scan confirmation", async () => {
    setupHubFetch({
      results: [
        {
          slug: "review-sentinel",
          display_name: "Review Sentinel",
          description: "Review pull requests before merge.",
          hub_name: "clawdhub",
          version: "1.2.0",
          score: 0.94,
          license: "Apache-2.0",
          content: "# Review Sentinel\n\nCheck patches carefully.",
        },
      ],
      scanResult: {
        is_safe: false,
        max_severity: "high",
        scan_duration_seconds: 0.02,
        findings_count: 1,
        findings: [
          {
            severity: "high",
            title: "Prompt injection",
            description: "The skill asks the agent to ignore local instructions.",
            remediation: "Remove the override language.",
            location: "line 4",
            category: "instruction-override",
          },
        ],
      },
    });

    const user = userEvent.setup();
    await searchHub(user);

    expect(screen.getAllByText("clawdhub").length).toBeGreaterThan(0);
    expect(screen.getByText("Apache-2.0")).toBeInTheDocument();
    expect(screen.getByText(/# Review Sentinel/)).toBeInTheDocument();
    expect(screen.getByText("SKILLSMP_API_KEY missing")).toBeInTheDocument();

    const installBeforeScan = screen.getByRole("button", { name: "Install hub skill" });
    expect(installBeforeScan).toBeDisabled();
    expect(screen.getByText("Run a safety scan before installing.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Scan hub skill" }));
    expect((await screen.findAllByText("HIGH")).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("Prompt injection")).toBeInTheDocument();
    expect(screen.getByText("Remove the override language.")).toBeInTheDocument();
    expect(screen.getByText("line 4")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Install hub skill" }));
    expect(
      await screen.findByRole("heading", { name: "Install despite HIGH findings?" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Install anyway" }));

    await waitFor(() =>
      expect(lastJsonBodyFor("/api/skills/hubs/install")).toEqual({
        hub_name: "clawdhub",
        slug: "review-sentinel",
        version: "1.2.0",
        project_id: "project-1",
      }),
    );
    expect(await screen.findByRole("radio", { name: "Installed" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(
      await screen.findByRole("button", { name: "Select Review Sentinel" }),
    ).toBeInTheDocument();
  });

  it("allows direct install after a passing safety scan", async () => {
    setupHubFetch({
      results: [
        {
          slug: "review-sentinel",
          display_name: "Review Sentinel",
          description: "Review pull requests before merge.",
          hub_name: "clawdhub",
          version: "1.2.0",
          score: 0.94,
          license: "Apache-2.0",
          content: "# Review Sentinel\n\nCheck patches carefully.",
        },
      ],
      scanResult: {
        is_safe: true,
        max_severity: "info",
        scan_duration_seconds: 0.01,
        findings_count: 0,
        findings: [],
      },
    });

    const user = userEvent.setup();
    await searchHub(user);

    await user.click(screen.getByRole("button", { name: "Scan hub skill" }));
    expect(await screen.findByText("SAFE")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Install hub skill" }));

    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: /Install despite/ })).not.toBeInTheDocument(),
    );
    expect(lastJsonBodyFor("/api/skills/hubs/install")?.hub_name).toBe("clawdhub");
  });

  it("keeps result rows scoped for accessible selection", async () => {
    setupHubFetch({
      results: [
        {
          slug: "review-sentinel",
          display_name: "Review Sentinel",
          description: "Review pull requests before merge.",
          hub_name: "clawdhub",
          version: "1.2.0",
          score: 0.94,
          content: "# Review Sentinel",
        },
      ],
      scanResult: {
        is_safe: true,
        max_severity: "info",
        scan_duration_seconds: 0.01,
        findings_count: 0,
        findings: [],
      },
    });

    const user = userEvent.setup();
    render(<SkillsTab />);

    await user.click(screen.getByRole("radio", { name: "Hub" }));
    await user.type(screen.getByRole("searchbox", { name: "Search hub skills" }), "review");
    fireEvent.click(screen.getByRole("button", { name: "Search hub skills" }));

    const list = await screen.findByRole("list", { name: "Hub search results" });
    const row = within(list).getByRole("button", { name: "Select Review Sentinel" });
    expect(row).toHaveTextContent("Review pull requests before merge.");
    expect(row).toHaveTextContent("clawdhub");
    expect(row).toHaveTextContent("v1.2.0");
  });
});
