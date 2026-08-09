import type { ReactElement, ReactNode } from "react";
import {
  fireEvent,
  render as baseRender,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ActivityActionButtons,
  ActivityActionsProvider,
} from "../../ActivityActionsContext";
import { ACTIVITY_PANEL_TABS } from "../../ActivityPanelTabs";
import { RulesTab } from "../../RulesTab";
import { nextCopyName } from "../RulesTabData";

// The tab's toolbar (selector / Filter / Search) renders in the shared panel
// header in the real layout; mount it alongside the tab so those controls are
// reachable in tests.
function HeaderHarness({ children }: { children: ReactNode }) {
  return (
    <ActivityActionsProvider>
      <ActivityActionButtons />
      {children}
    </ActivityActionsProvider>
  );
}

const render = (ui: ReactElement) =>
  baseRender(ui, { wrapper: HeaderHarness });

vi.mock("../../../../hooks/useWebSocketEvent", () => ({
  useWebSocketEvent: vi.fn(),
}));

vi.mock("../../../shared/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

vi.mock("../../../shared/CodeMirrorEditor", () => ({
  CodeMirrorEditor: ({
    content,
    onChange,
    readOnly,
  }: {
    content: string;
    onChange?: (content: string) => void;
    readOnly?: boolean;
  }) => (
    <textarea
      aria-label="Rule YAML"
      readOnly={readOnly}
      value={content}
      onChange={(event) => onChange?.(event.target.value)}
    />
  ),
}));

type RuleRecord = {
  id: string;
  name: string;
  description: string | null;
  event: string | null;
  group: string | null;
  when: string | null;
  enabled: boolean;
  priority: number;
  source: string;
  tags: string[] | null;
  effects: Array<Record<string, unknown>> | null;
  match: Record<string, unknown> | null;
  audience?: string | null;
  agent_scope?: string[] | null;
};

type FetchCall = {
  url: string;
  method: string;
  body: unknown;
};

const ORIGINAL_FETCH = globalThis.fetch;

function makeRule(overrides: Partial<RuleRecord>): RuleRecord {
  return {
    id: "rule-1",
    name: "alpha-rule",
    description: "Blocks edits to generated files",
    event: "before_tool",
    group: "guardrails",
    when: "tool.name == 'Edit'",
    enabled: true,
    priority: 50,
    source: "project",
    tags: ["safety"],
    effects: [{ type: "block", reason: "Generated file" }],
    match: { tools: ["Edit"] },
    audience: "interactive",
    agent_scope: ["developer"],
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installRulesFetch({ toggleSucceeds = true }: { toggleSucceeds?: boolean } = {}) {
  const rules = [
    makeRule({ id: "rule-1", name: "alpha-rule" }),
    makeRule({
      id: "rule-2",
      name: "beta-rule",
      description: "Paused hook",
      enabled: false,
      event: "stop",
      group: "lifecycle",
      source: "installed",
      tags: ["pause"],
      priority: 80,
      effects: [{ type: "set_variable", variable: "paused", value: true }],
    }),
    makeRule({
      id: "rule-3",
      name: "gamma-rule",
      description: "Compaction guard",
      event: "pre_compact",
      group: "context",
      source: "installed",
      tags: ["memory"],
    }),
    makeRule({
      id: "rule-4",
      name: "template-rule",
      description: "Bundled template",
      event: "before_tool",
      group: "guardrails",
      source: "template",
      tags: ["template"],
    }),
  ];
  const calls: FetchCall[] = [];
  let copyAttempts = 0;

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const requestUrl = new URL(url, "http://localhost");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method, body });

    if (requestUrl.pathname === "/api/rules/groups") {
      return jsonResponse({ groups: ["context", "guardrails", "lifecycle"] });
    }

    if (requestUrl.pathname === "/api/rules" && method === "GET") {
      return jsonResponse({ rules: [...rules], enforcement_enabled: true });
    }

    if (requestUrl.pathname === "/api/rules" && method === "PUT") {
      return jsonResponse({ status: "success", enforcement_enabled: body.enforcement_enabled });
    }

    if (requestUrl.pathname === "/api/rules" && method === "POST") {
      copyAttempts += 1;
      if (copyAttempts === 1) {
        return jsonResponse({ detail: "Rule already exists" }, 409);
      }
      const created = makeRule({
        id: "rule-copy",
        name: body.name,
        description: body.definition.description,
        event: body.definition.event,
        group: body.definition.group,
        enabled: body.definition.enabled,
        priority: body.definition.priority,
        source: "project",
        tags: body.definition.tags,
        effects: body.definition.effects,
      });
      rules.push(created);
      return jsonResponse({ status: "success", rule: created }, 201);
    }

    const detailMatch = requestUrl.pathname.match(/^\/api\/rules\/([^/]+)$/);
    if (detailMatch && method === "GET") {
      const ruleName = decodeURIComponent(detailMatch[1]);
      const rule = rules.find((candidate) => candidate.name === ruleName);
      return rule
        ? jsonResponse({ status: "success", rule })
        : jsonResponse({ detail: "not found" }, 404);
    }

    if (detailMatch && method === "PUT") {
      const ruleName = decodeURIComponent(detailMatch[1]);
      const index = rules.findIndex((candidate) => candidate.name === ruleName);
      if (index < 0) return jsonResponse({ detail: "not found" }, 404);
      const updated = {
        ...rules[index],
        ...body.definition,
        id: rules[index].id,
        source: rules[index].source,
        name: body.name ?? rules[index].name,
      };
      rules[index] = updated;
      return jsonResponse({ status: "success", rule: updated });
    }

    if (requestUrl.pathname.endsWith("/toggle") && method === "PUT") {
      if (!toggleSucceeds) return jsonResponse({ detail: "toggle failed" }, 500);
      const pathParts = requestUrl.pathname.split("/");
      const ruleName = decodeURIComponent(pathParts[pathParts.length - 2] ?? "");
      const rule = rules.find((candidate) => candidate.name === ruleName);
      if (rule) rule.enabled = body.enabled;
      return jsonResponse({ status: "success" });
    }

    if (detailMatch && method === "DELETE") {
      const ruleName = decodeURIComponent(detailMatch[1]);
      const index = rules.findIndex((candidate) => candidate.name === ruleName);
      if (index >= 0) rules.splice(index, 1);
      return jsonResponse({ status: "success" });
    }

    return jsonResponse({ detail: `Unhandled ${method} ${requestUrl.pathname}` }, 404);
  });

  globalThis.fetch = fetchMock as unknown as typeof fetch;
  return { calls, fetchMock, rules };
}

describe("Rules activity tab", () => {
  beforeEach(() => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    globalThis.fetch = ORIGINAL_FETCH;
  });

  it("generates stable copy names", () => {
    expect(nextCopyName("alpha", new Set(["alpha"]))).toBe("alpha-copy");
    expect(nextCopyName("alpha", new Set(["alpha", "alpha-copy"]))).toBe("alpha-copy-2");
    expect(nextCopyName("alpha-copy", new Set(["alpha-copy", "alpha-copy-2"]))).toBe(
      "alpha-copy-3",
    );
  });

  it("registers the rules tab", () => {
    expect(ACTIVITY_PANEL_TABS.some((tab) => tab.id === "rules")).toBe(true);
  });

  it("composes status, search, event, group, source, and tag filters", async () => {
    installRulesFetch();
    const user = userEvent.setup();

    render(<RulesTab />);

    const rulesList = await screen.findByRole("list", { name: "Rules" });
    expect(within(rulesList).getByText("alpha-rule")).toBeInTheDocument();
    expect(within(rulesList).getByText("gamma-rule")).toBeInTheDocument();
    expect(within(rulesList).queryByText("beta-rule")).not.toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: "Disabled" }));
    expect(await within(rulesList).findByText("beta-rule")).toBeInTheDocument();
    expect(within(rulesList).queryByText("alpha-rule")).not.toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: "Enabled" }));
    // The search bar is hidden until the header Search toggle opens it.
    await user.click(screen.getByRole("button", { name: "Search rules" }));
    await user.type(screen.getByRole("searchbox", { name: "Search rules" }), "compaction");
    expect(await within(rulesList).findByText("gamma-rule")).toBeInTheDocument();
    expect(within(rulesList).queryByText("alpha-rule")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Filter rules" }));
    expect(screen.queryByRole("searchbox", { name: "Search rules" })).not.toBeInTheDocument();
    expect(await within(rulesList).findByText("alpha-rule")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Filter by event"), "before_tool");
    await user.selectOptions(screen.getByLabelText("Filter by group"), "guardrails");
    await user.selectOptions(screen.getByLabelText("Filter by source"), "project");
    await user.selectOptions(screen.getByLabelText("Filter by tag"), "safety");

    expect(await within(rulesList).findByText("alpha-rule")).toBeInTheDocument();
    expect(within(rulesList).queryByText("gamma-rule")).not.toBeInTheDocument();
    expect(within(rulesList).queryByText("template-rule")).not.toBeInTheDocument();
  });

  it("preserves popup close, reset, outside-click, Escape, and focus transitions", async () => {
    installRulesFetch();
    const user = userEvent.setup();

    render(<RulesTab />);

    const rulesList = await screen.findByRole("list", { name: "Rules" });
    const trigger = screen.getByRole("button", { name: "Filter rules" });

    await user.click(trigger);
    expect(screen.getByRole("dialog", { name: "Rule filters" })).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Filter by event"), "before_tool");
    expect(within(rulesList).queryByText("gamma-rule")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Reset rule filters" }));
    expect(screen.getByLabelText("Filter by event")).toHaveValue("");
    expect(await within(rulesList).findByText("gamma-rule")).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Rule filters" })).not.toBeInTheDocument(),
    );
    expect(trigger).toHaveFocus();

    await user.click(trigger);
    fireEvent.click(screen.getByTestId("rules-filter-overlay"));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Rule filters" })).not.toBeInTheDocument(),
    );
    expect(trigger).toHaveFocus();

    await user.click(trigger);
    await user.click(trigger);
    expect(screen.queryByRole("dialog", { name: "Rule filters" })).not.toBeInTheDocument();
  });

  it("selects the first rule by default so the detail pane is populated (#19152)", async () => {
    installRulesFetch();
    render(<RulesTab />);

    expect(await screen.findByLabelText("Rule name")).toHaveValue("alpha-rule");
    expect(screen.getByRole("switch", { name: "Rule enabled" })).toBeChecked();
  });

  it("exposes row actions and retries copy name collisions once", async () => {
    const { calls } = installRulesFetch();
    const user = userEvent.setup();

    render(<RulesTab />);
    const rulesList = await screen.findByRole("list", { name: "Rules" });
    expect(within(rulesList).getByText("alpha-rule")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Open actions for alpha-rule" }));
    const firstMenu = screen.getByRole("menu", { name: "Actions for alpha-rule" });
    expect(within(firstMenu).getByRole("menuitem", { name: "Disable" })).toBeInTheDocument();
    expect(within(firstMenu).getByRole("menuitem", { name: "Copy" })).toBeInTheDocument();
    expect(within(firstMenu).getByRole("menuitem", { name: "Delete" })).toBeInTheDocument();
    await user.click(within(firstMenu).getByRole("menuitem", { name: "Disable" }));

    await waitFor(() =>
      expect(calls.some((call) => call.url.includes("/api/rules/alpha-rule/toggle"))).toBe(true),
    );
    expect(
      calls.find((call) => call.url.includes("/api/rules/alpha-rule/toggle"))?.body,
    ).toEqual({ enabled: false });

    await user.click(screen.getByRole("radio", { name: "Disabled" }));
    expect(await within(rulesList).findByText("alpha-rule")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open actions for alpha-rule" }));
    await user.click(screen.getByRole("menuitem", { name: "Copy" }));

    await waitFor(() =>
      expect(screen.getAllByText("alpha-rule-copy-2").length).toBeGreaterThan(0),
    );
    const postBodies = calls
      .filter((call) => call.url.endsWith("/api/rules") && call.method === "POST")
      .map((call) => call.body);
    expect(postBodies).toEqual([
      expect.objectContaining({ name: "alpha-rule-copy" }),
      expect.objectContaining({ name: "alpha-rule-copy-2" }),
    ]);
  });

  it("shows an error and preserves the enabled state when a toggle fails", async () => {
    installRulesFetch({ toggleSucceeds: false });
    const user = userEvent.setup();

    render(<RulesTab />);
    const rulesList = await screen.findByRole("list", { name: "Rules" });
    expect(within(rulesList).getByText("alpha-rule")).toBeInTheDocument();
    const alphaRule = screen.getByRole("button", { name: "Select alpha-rule" });
    expect(within(alphaRule).getByLabelText("Rule enabled")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Open actions for alpha-rule" }));
    await user.click(screen.getByRole("menuitem", { name: "Disable" }));

    expect(await screen.findByText("Failed to disable rule")).toBeInTheDocument();
    expect(within(alphaRule).getByLabelText("Rule enabled")).toBeInTheDocument();
  });

  it("saves scalar draft edits as a full-definition PUT and reselects renamed rules", async () => {
    const { calls } = installRulesFetch();
    const user = userEvent.setup();

    render(<RulesTab />);
    await user.click(await screen.findByRole("button", { name: /Select alpha-rule/i }));

    const nameField = await screen.findByLabelText("Rule name");
    await user.clear(nameField);
    await user.type(nameField, "alpha-renamed");
    await user.clear(screen.getByLabelText("Description"));
    await user.type(screen.getByLabelText("Description"), "Edited description");
    await user.clear(screen.getByLabelText("Priority"));
    await user.type(screen.getByLabelText("Priority"), "42");
    await user.selectOptions(screen.getByLabelText("Audience"), "autonomous");

    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(screen.getByDisplayValue("alpha-renamed")).toBeInTheDocument());
    const putCall = calls.find(
      (call) => call.url.endsWith("/api/rules/alpha-rule") && call.method === "PUT",
    );
    expect(putCall?.body).toEqual({
      name: "alpha-renamed",
      definition: expect.objectContaining({
        description: "Edited description",
        event: "before_tool",
        group: "guardrails",
        priority: 42,
        tags: ["safety"],
        audience: "autonomous",
        agent_scope: ["developer"],
        enabled: true,
        effects: [{ type: "block", reason: "Generated file" }],
        match: { tools: ["Edit"] },
        when: "tool.name == 'Edit'",
      }),
    });
    expect((putCall?.body as { definition?: Record<string, unknown> }).definition).not.toHaveProperty(
      "name",
    );
  });

  it("edits full rule definitions from the YAML detail view", async () => {
    const { calls } = installRulesFetch();
    const user = userEvent.setup();

    render(<RulesTab />);
    await user.click(await screen.findByRole("button", { name: /Select alpha-rule/i }));
    await screen.findByLabelText("Rule name");

    await user.click(screen.getByRole("radio", { name: "YAML" }));
    const yamlEditor = await screen.findByLabelText("Rule YAML");
    expect((yamlEditor as HTMLTextAreaElement).value).toContain("effects:");
    expect((yamlEditor as HTMLTextAreaElement).value).toContain("match:");
    // Read-only by default — an explicit Edit click opens the buffered editor.
    expect(yamlEditor).toHaveAttribute("readonly");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByLabelText("Rule YAML")).not.toHaveAttribute("readonly");

    fireEvent.change(screen.getByLabelText("Rule YAML"), {
      target: {
        value: [
          "name: alpha-yaml",
          "description: YAML description",
          "event: before_tool",
          "group: guardrails",
          "priority: 77",
          "enabled: true",
          "tags:",
          "  - safety",
          "audience: interactive",
          "agent_scope:",
          "  - developer",
          'when: session.phase == "edit"',
          "match:",
          "  tools:",
          "    - Edit",
          "    - MultiEdit",
          "effects:",
          "  - type: block",
          "    reason: YAML generated file",
          "",
        ].join("\n"),
      },
    });

    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(screen.getAllByText("alpha-yaml").length).toBeGreaterThan(0));
    const putCall = calls.find(
      (call) => call.url.endsWith("/api/rules/alpha-rule") && call.method === "PUT",
    );
    expect(putCall?.body).toEqual({
      name: "alpha-yaml",
      definition: expect.objectContaining({
        description: "YAML description",
        event: "before_tool",
        group: "guardrails",
        priority: 77,
        enabled: true,
        tags: ["safety"],
        audience: "interactive",
        agent_scope: ["developer"],
        when: 'session.phase == "edit"',
        match: { tools: ["Edit", "MultiEdit"] },
        effects: [{ type: "block", reason: "YAML generated file" }],
      }),
    });
    expect((putCall?.body as { definition?: Record<string, unknown> }).definition).not.toHaveProperty(
      "name",
    );
  });

  it("keeps bundled template rule names read-only", async () => {
    installRulesFetch();
    const user = userEvent.setup();

    render(<RulesTab />);
    await user.click(await screen.findByRole("button", { name: /Select template-rule/i }));

    expect(await screen.findByText("Bundled template rule names are read-only")).toBeInTheDocument();
    expect(screen.getAllByText("template-rule").length).toBeGreaterThan(1);
    expect(screen.queryByLabelText("Rule name")).not.toBeInTheDocument();
  });
});
