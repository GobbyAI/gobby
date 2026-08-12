import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const cwd = process.cwd();

const TYPOGRAPHY_ROOTS = ["src/styles", "src/components"];
const SANCTIONED_TOKEN_FILES = new Set(["src/styles/tokens.css"]);

function readSource(rel: string): string {
  return readFileSync(join(cwd, rel), "utf8");
}

function readSessionsSurfaceSource(): string {
  return [
    "src/components/activity/SessionsTab.tsx",
    "src/components/activity/SessionsTabList.tsx",
    "src/components/activity/SessionsTabDetail.tsx",
  ]
    .map(readSource)
    .join("\n");
}

function readTsxSources(rel: string): Array<[string, string]> {
  return readdirSync(join(cwd, rel), { withFileTypes: true }).flatMap(
    (entry) => {
      const child = join(rel, entry.name);
      if (entry.isDirectory()) {
        return entry.name === "__tests__" || entry.name === "__visual__"
          ? []
          : readTsxSources(child);
      }
      return entry.name.endsWith(".tsx") ? [[child, readSource(child)]] : [];
    },
  );
}

function sourceFilesUnder(rel: string): string[] {
  return readdirSync(join(cwd, rel), { withFileTypes: true }).flatMap(
    (entry) => {
      const child = join(rel, entry.name);
      if (entry.isDirectory()) {
        return entry.name === "__tests__" || entry.name === "__visual__"
          ? []
          : sourceFilesUnder(child);
      }
      return /\.(?:css|ts|tsx)$/.test(entry.name) ? [child] : [];
    },
  );
}

describe("activity-panel typography ladder (#14245)", () => {
  it("keeps live component typography on the shared token ladder", () => {
    const offLadder = TYPOGRAPHY_ROOTS.flatMap(sourceFilesUnder)
      .filter((rel) => !SANCTIONED_TOKEN_FILES.has(rel))
      .flatMap((rel) => {
        const source = readSource(rel);
        const patterns = [
          /font-size\s*:\s*(?:calc\(|var\(--font-size-base\)|[0-9])/gi,
          /fontSize\s*:\s*['"](?:calc\(|[0-9])/g,
          /text-\[(?:length:calc\(|[0-9])/g,
        ];
        return patterns.flatMap((pattern) =>
          Array.from(source.matchAll(pattern), (match) => `${rel}:${match[0]}`),
        );
      });

    expect(offLadder).toEqual([]);
  });

  it("exposes shared row-title and row-meta utility classes locked to the ladder", () => {
    const source = readSource("src/components/activity/ActivityPanel.tsx");

    expect(source).toContain(
      "[&_.activity-row-title]:text-[length:var(--text-base)]",
    );
    expect(source).toContain(
      "[&_.activity-row-title]:font-[var(--font-weight-medium)]",
    );
    expect(source).toContain(
      "[&_.activity-row-meta]:text-[length:var(--text-sm)]",
    );
    expect(source).toContain(
      "[&_.activity-row-meta]:font-[var(--font-weight-normal)]",
    );
  });

  it("uses one shared activity status-bar height and readable title size", () => {
    const rootSource = readSource("src/styles/tokens.css");
    const activitySource = readSource(
      "src/components/activity/ActivityPanel.tsx",
    );
    const mcpDetailSource = readSource(
      "src/components/activity/mcp/McpDetailPanel.tsx",
    );
    const taskSource = readSource("src/components/activity/TasksTab.tsx");
    const sessionsSource = readSessionsSurfaceSource();

    // Canonical token lives in :root (src/styles/tokens.css) so .command-bar,
    // .agent-status-bar, .voice-status-bar, and the activity-panel bars all
    // inherit the same height. Inner scopes must not redeclare it.
    expect(rootSource).toContain("--activity-panel-bar-height: 2.75rem");
    expect(activitySource).not.toContain("[--activity-panel-bar-height:");
    expect(activitySource).toContain(
      "[&_.activity-panel-status-bar]:min-h-[var(--activity-panel-bar-height)]",
    );
    expect(activitySource).toContain(
      String.raw`[&_.activity-panel-status-bar\_\_title]:text-[length:var(--text-base)]`,
    );
    expect(activitySource).toContain(
      String.raw`[&_.activity-panel-status-bar\_\_title]:font-[var(--font-weight-medium)]`,
    );
    expect(mcpDetailSource).toContain(
      "min-h-[var(--activity-panel-bar-height)]",
    );
    expect(mcpDetailSource).toContain("text-[length:var(--text-base)]");
    expect(mcpDetailSource).toContain("font-[var(--font-weight-medium)]");
    expect(taskSource).toContain(
      "min-h-[var(--activity-panel-bar-height,2.5rem)]",
    );
    expect(sessionsSource).toContain("activity-panel-status-bar__title");
  });

  it("keeps status-bar controls at desktop visual height on touch devices", () => {
    const rootSource = readSource("src/styles/tokens.css");
    const activitySource = readSource(
      "src/components/activity/ActivityPanel.tsx",
    );
    const filterPrimitivesSource = readSource(
      "src/components/activity/FilterPrimitives.tsx",
    );
    const commandBarSource = readSource("src/components/chat/CommandBar.tsx");
    const statusBarSource = readSource(
      "src/components/chat/AgentStatusBar.tsx",
    );

    expect(rootSource).toContain("--status-bar-control-height: 1.75rem");
    // No coarse-pointer promotion of the compact control row: the bar
    // supplies the 44px row and coarseHitAreaCls floors the tap target, so
    // compact controls keep their desktop visual height on touch (#19181).
    expect(rootSource).not.toContain("--control-row-height-sm: 2.75rem");
    // Status-bar session actions encode the desktop-height-on-touch contract
    // via the Button `dense` prop (min-h-7 with no pointer-coarse promotion).
    expect(statusBarSource).toMatch(/variant="accent"\s+size="sm"\s+dense/);
    expect(commandBarSource).toContain(
      "min-h-[var(--status-bar-control-height)]",
    );
    expect(activitySource).toContain("activity-panel-tabs");
    expect(activitySource).toContain("px-3");
    expect(filterPrimitivesSource).toContain("relative aria-expanded:border-");
    expect(filterPrimitivesSource).toContain("data-filter-active-count");
    expect(filterPrimitivesSource).toContain("absolute -top-1 -right-1");

    const filterButtonAuthors = readTsxSources("src")
      .filter(([, source]) => source.includes("activity-filter-button"))
      .map(([path]) => path);
    expect(filterButtonAuthors).toEqual([]);
  });

  it("keeps chat status-bar typography on the shared component utility ladder", () => {
    const agentStatusSource = readSource(
      "src/components/chat/AgentStatusBar.tsx",
    );
    const voiceStatusSource = readSource(
      "src/components/chat/VoiceStatusBar.tsx",
    );

    expect(agentStatusSource).toContain("text-[length:var(--text-sm)]");
    expect(voiceStatusSource).toContain("text-[length:var(--text-xs)]");
  });

  it("locks the tasks row title to --text-base / medium", () => {
    const source = readSource("src/components/activity/TaskTreeRow.tsx");

    expect(source).toContain("text-[length:var(--text-base)]");
    expect(source).toContain("font-[var(--font-weight-medium)]");
    expect(source).toContain("text-[length:var(--text-sm)]");
    expect(source).toContain("pointer-coarse:min-h-11");
  });

  it("keeps high/critical priority tasks bold while raising the default to medium", () => {
    const source = readSource("src/components/activity/TasksTabModel.ts");

    const match = source.match(
      /PRIORITY_TEXT_WEIGHTS:\s*Record<number,\s*string>\s*=\s*{([\s\S]*?)}/,
    );
    expect(match).not.toBeNull();
    const body = match![1];

    expect(body).toMatch(/0:\s*["']var\(--font-weight-semibold\)["']/);
    expect(body).toMatch(/1:\s*["']var\(--font-weight-semibold\)["']/);
    expect(body).toMatch(/2:\s*["']var\(--font-weight-medium\)["']/);
    expect(body).toMatch(/3:\s*["']var\(--font-weight-medium\)["']/);
    expect(body).toMatch(/4:\s*["']var\(--font-weight-medium\)["']/);
    expect(body).not.toMatch(/var\(--font-weight-normal\)/);
  });

  it("routes Sessions/Pipelines/Cron row titles through activity-row-title", () => {
    const sessions = readSessionsSurfaceSource();
    const pipelines = readSource("src/components/activity/PipelinesTab.tsx");
    const cron = readSource("src/components/activity/CronTab.tsx");

    expect(sessions).toContain("activity-row-title");
    expect(pipelines).toContain("activity-row-title");
    expect(cron).toContain("activity-row-title");

    expect(sessions).not.toMatch(
      /className="text-sm text-foreground truncate"/,
    );
    expect(pipelines).not.toMatch(
      /className="text-sm text-foreground truncate"/,
    );
    expect(cron).not.toMatch(/className="text-sm text-foreground truncate"/);
  });

  it("routes Pipelines/Cron meta timestamps through activity-row-meta", () => {
    const pipelines = readSource("src/components/activity/PipelinesTab.tsx");
    const cron = readSource("src/components/activity/CronTab.tsx");

    expect(pipelines).toContain("activity-row-meta");
    expect(cron).toContain("activity-row-meta");

    expect(pipelines).not.toMatch(
      /text-\[10px\] text-muted-foreground shrink-0/,
    );
    expect(cron).not.toMatch(
      /text-\[10px\] text-muted-foreground tabular-nums/,
    );
  });

  it("locks cron run rows to the meta token", () => {
    const source = readSource("src/components/activity/CronTab.tsx");

    expect(source).toContain("text-[length:var(--text-sm)]");
    expect(source).toContain("font-[var(--font-weight-normal)]");
    expect(source).not.toContain("cron-tab-run");
  });

  it("locks files-tab tree rows to --text-base and meta size to --text-sm", () => {
    const source = readSource("src/components/activity/FilesTabTree.tsx");

    expect(source).toContain("text-[length:var(--text-base)]");
    expect(source).toContain("font-[var(--font-weight-medium)]");
    expect(source).toContain("text-[length:var(--text-sm)]");
    expect(source).not.toContain("files-tree-item");
    expect(source).not.toContain("files-tree-loading");
  });

  it("locks the activity-tab-empty body and provides heading helpers (chat empty-state parity)", () => {
    const source = readSource("src/components/activity/ActivityPanelEmpty.tsx");

    expect(source).toContain("text-[length:var(--text-base)]");
    expect(source).toContain("font-[var(--font-weight-normal)]");
    expect(source).toContain("text-[length:var(--text-xl)]");
    expect(source).toContain("text-[var(--text-secondary)]");
    expect(source).toContain("text-[var(--text-muted)]");
  });

  it("locks the chat empty-state title and copy to the same utility ladder", () => {
    const source = readSource("src/components/chat/MessageList.tsx");
    const commandPaletteSource = readSource(
      "src/components/chat/CommandPalette.tsx",
    );

    expect(source).toContain(
      "chat-empty-state flex flex-col items-center gap-3 text-center",
    );
    expect(source).toContain(
      "chat-empty-state__title text-[length:var(--text-xl)] text-[var(--text-secondary)]",
    );
    expect(source).toContain(
      "chat-empty-state__copy max-w-[26rem] text-[length:var(--text-base)] text-[var(--text-muted)]",
    );
    expect(commandPaletteSource).toContain(
      "command-palette-empty p-6 text-center text-[length:var(--text-sm)] text-[var(--text-muted)]",
    );
  });

  it("keeps TSX typography on the shared ladder", () => {
    for (const [path, source] of readTsxSources("src")) {
      expect(source, path).not.toMatch(/text-\[\d+(?:\.\d+)?px\]/);
      expect(source, path).not.toMatch(
        /fontSize:\s*(?:["']\d+(?:\.\d+)?(?:px|rem)["']|\d+)/,
      );
    }
  });
});
