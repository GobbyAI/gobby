import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { RuleSummary } from "../hooks/useRules";
import { RulesTabList } from "../components/activity/rules/RulesTabList";

// #20050: bare auto-track grids (`grid` with no column definition) size their
// track to the widest row's intrinsic width, so long unbreakable content gives
// the whole pane a horizontal scrollbar. Stack grids in scroll panes must
// declare a shrinkable `grid-cols-1` track (or use a flex column), and long
// tokens in flex rows must truncate.

function isWebPackageRoot(path: string): boolean {
  return (
    existsSync(join(path, "package.json")) &&
    existsSync(join(path, "src/main.tsx"))
  );
}

function resolveWebPackageRoot(): string {
  const current = process.cwd();
  if (isWebPackageRoot(current)) return current;
  const fallback = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
  if (isWebPackageRoot(fallback)) return fallback;
  throw new Error(`Unable to resolve web package root from cwd=${current}`);
}

let webPackageRoot: string | undefined;

function readSource(rel: string): string {
  webPackageRoot ??= resolveWebPackageRoot();
  return readFileSync(join(webPackageRoot, rel), "utf8");
}

function makeRule(overrides: Partial<RuleSummary>): RuleSummary {
  return {
    id: "rule-1",
    name: "rule",
    description: null,
    event: null,
    group: null,
    when: null,
    enabled: true,
    priority: 0,
    source: "project",
    tags: null,
    ...overrides,
  };
}

describe("horizontal overflow guards (#20050)", () => {
  it("renders the Rules list as a flex column, not a bare auto-track grid", () => {
    const rules = [
      makeRule({
        id: "r1",
        name: "structured-handoff-with-a-very-long-name",
        event: "after_tool",
        group: "context-handoff",
      }),
      makeRule({ id: "r2", name: "short" }),
    ];
    render(
      <RulesTabList
        rules={rules}
        selectedName={null}
        busyRuleName={null}
        onSelect={vi.fn()}
        onToggle={vi.fn()}
        onCopy={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    const list = screen.getByRole("list", { name: "Rules" });
    expect(list).toHaveClass("flex", "flex-col");
    expect(list.classList.contains("grid")).toBe(false);
  });

  it("keeps detail-panel stack grids on a shrinkable grid-cols-1 track", () => {
    const agents = readSource(
      "src/components/activity/agents/AgentsDetailPanel.tsx",
    );
    expect(agents).toContain("grid grid-cols-1 gap-3 md:grid-cols-2");
    expect(agents).toContain("mt-4 grid grid-cols-1 gap-4");

    const channel = readSource(
      "src/components/activity/integrations/ChannelDetailPanel.tsx",
    );
    expect(channel).not.toContain('className="grid gap-3"');
    expect(channel).toContain("grid grid-cols-1 gap-3");
    expect(channel).toContain("mt-3 grid grid-cols-1 gap-2");

    const messages = readSource(
      "src/components/activity/integrations/MessagesView.tsx",
    );
    expect(messages).toContain("grid grid-cols-1 gap-2");
  });

  it("truncates stored-secret names so long tokens cannot widen the dialog", () => {
    const secrets = readSource(
      "src/components/settings/sections/SecretsAuthSection.tsx",
    );
    expect(secrets).toContain("min-w-0 flex-1 truncate");
  });

  it("styles the horizontal scrollbar height to match the vertical treatment", () => {
    const base = readSource("src/styles/base.css");
    const scrollbarRule =
      base.match(/::-webkit-scrollbar\s*\{[^}]*\}/)?.[0] ?? "";
    expect(scrollbarRule).toContain("width: 0.5rem");
    expect(scrollbarRule).toContain("height: 0.5rem");
  });
});
