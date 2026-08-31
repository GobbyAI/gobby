import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  useProjects,
  type ProjectWithStats,
} from "../../../../hooks/useProjects";
import { DateTimeField } from "../DateTimeField";
import {
  localInputValueToUtcIso,
  utcIsoToLocalInputValue,
} from "../dateTimeConversion";
import { ProjectSelectField } from "../ProjectSelectField";

vi.mock("../../../../hooks/useProjects", () => ({
  useProjects: vi.fn(),
}));

type ProjectsHookState = ReturnType<typeof useProjects> & { error?: unknown };

function makeProject(overrides: Partial<ProjectWithStats>): ProjectWithStats {
  return {
    id: "project-1",
    name: "alpha",
    display_name: "Alpha workspace",
    checkout: null,
    github_url: null,
    github_repo: null,
    linear_team_id: null,
    linear_project_id: null,
    approval_rules: [],
    validation_detection: null,
    created_at: "2026-01-01T00:00:00.000Z",
    updated_at: "2026-01-01T00:00:00.000Z",
    session_count: 0,
    open_task_count: 0,
    last_activity_at: null,
    ...overrides,
  };
}

function makeProjectsState(
  overrides: Partial<ProjectsHookState> = {},
): ProjectsHookState {
  const projects = [
    makeProject({
      id: "11111111-1111-4111-8111-111111111111",
      display_name: "Client portal",
      name: "client-portal",
    }),
    makeProject({
      id: "22222222-2222-4222-8222-222222222222",
      display_name: "",
      name: "ops-console",
    }),
  ];

  return {
    projects,
    allProjects: projects,
    isLoading: false,
    error: null,
    selectedProject: null,
    selectedProjectId: null,
    activeSubTab: "overview",
    setActiveSubTab: vi.fn(),
    searchText: "",
    setSearchText: vi.fn(),
    selectProject: vi.fn(),
    deselectProject: vi.fn(),
    updateProject: vi.fn(),
    deleteProject: vi.fn(),
    refresh: vi.fn(),
    totalSessions: 0,
    totalOpenTasks: 0,
    ...overrides,
  };
}

afterEach(() => {
  document.documentElement.removeAttribute("data-theme");
  vi.clearAllMocks();
});

describe("ProjectSelectField (#17015)", () => {
  it("lists project options by display name or name and emits UUID values", () => {
    vi.mocked(useProjects).mockReturnValue(makeProjectsState());
    const onChange = vi.fn();

    render(
      <ProjectSelectField
        label="Project"
        ariaLabel="Project"
        value="22222222-2222-4222-8222-222222222222"
        onChange={onChange}
      />,
    );

    const select = screen.getByLabelText("Project");
    expect(
      within(select).getByRole("option", { name: "Client portal" }),
    ).toHaveValue("11111111-1111-4111-8111-111111111111");
    expect(
      within(select).getByRole("option", { name: "ops-console" }),
    ).toHaveValue("22222222-2222-4222-8222-222222222222");

    fireEvent.change(select, {
      target: { value: "11111111-1111-4111-8111-111111111111" },
    });

    expect(onChange).toHaveBeenCalledWith(
      "11111111-1111-4111-8111-111111111111",
    );
  });

  it("keeps an unknown selected project visible by UUID prefix", () => {
    vi.mocked(useProjects).mockReturnValue(makeProjectsState());

    render(
      <ProjectSelectField
        label="Project"
        ariaLabel="Project"
        value="99999999-9999-4999-8999-999999999999"
        onChange={vi.fn()}
      />,
    );

    expect(
      within(screen.getByLabelText("Project")).getByRole("option", {
        name: "Unknown project (99999999)",
      }),
    ).toHaveValue("99999999-9999-4999-8999-999999999999");
  });

  it("degrades loading and error states to disabled hint options", () => {
    vi.mocked(useProjects).mockReturnValue(
      makeProjectsState({ isLoading: true }),
    );
    const { rerender } = render(
      <ProjectSelectField
        label="Project"
        ariaLabel="Project"
        value=""
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByLabelText("Project")).toBeDisabled();
    expect(
      within(screen.getByLabelText("Project")).getByRole("option"),
    ).toHaveTextContent("Loading projects");

    vi.mocked(useProjects).mockReturnValue(
      makeProjectsState({ error: new Error("Project registry unavailable") }),
    );
    rerender(
      <ProjectSelectField
        label="Project"
        ariaLabel="Project"
        value=""
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByLabelText("Project")).toBeDisabled();
    expect(
      within(screen.getByLabelText("Project")).getByRole("option"),
    ).toHaveTextContent("Projects unavailable");
  });
});

describe("DateTimeField (#17015)", () => {
  it("round-trips UTC ISO values through local inputs without DST drift", () => {
    const boundaryInstants = [
      "2026-03-08T07:30:00.000Z",
      "2026-11-01T06:30:00.000Z",
    ];

    for (const isoValue of boundaryInstants) {
      const localValue = utcIsoToLocalInputValue(isoValue);

      expect(localValue).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);
      expect(localInputValueToUtcIso(localValue, isoValue)).toBe(isoValue);
    }
  });

  it("emits UTC ISO strings when the native datetime input changes", () => {
    const onChange = vi.fn();
    render(
      <DateTimeField
        label="Start"
        ariaLabel="Start"
        value="2026-03-08T07:30:00.000Z"
        onChange={onChange}
      />,
    );

    fireEvent.change(screen.getByLabelText("Start"), {
      target: { value: "2026-03-09T09:45" },
    });

    expect(onChange).toHaveBeenCalledWith(
      localInputValueToUtcIso("2026-03-09T09:45"),
    );
  });

  it("drives native picker color-scheme from the resolved theme", async () => {
    render(
      <DateTimeField
        label="Start"
        ariaLabel="Start"
        value="2026-03-08T07:30:00.000Z"
        onChange={vi.fn()}
      />,
    );

    const input = screen.getByLabelText("Start") as HTMLInputElement;
    expect(input.type).toBe("datetime-local");
    expect(input.style.colorScheme).toBe("dark");

    await act(async () => {
      document.documentElement.setAttribute("data-theme", "light");
      await Promise.resolve();
    });
    await waitFor(() => expect(input.style.colorScheme).toBe("light"));

    await act(async () => {
      document.documentElement.removeAttribute("data-theme");
      await Promise.resolve();
    });
    await waitFor(() => expect(input.style.colorScheme).toBe("dark"));
  });
});
