import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { renderBadges } from "../SessionsTab.helpers";
import type {
  Badge,
  WatchingSessionEntry,
} from "../SessionsTab.helpers";
import type { AcpSessionInfo } from "../../../types/sessions";

function makeEntry(
  overrides: Partial<WatchingSessionEntry> = {},
): WatchingSessionEntry {
  return {
    id: "entry-1",
    type: "session",
    label: "#1: Session",
    provider: "qwen",
    status: "active",
    sessionType: "web_chat",
    inputTokens: 0,
    outputTokens: 0,
    totalTokens: 0,
    hasTmux: false,
    sandboxEnabled: false,
    isLocal: false,
    ...overrides,
  };
}

function acpBlock(
  capabilities: Partial<AcpSessionInfo["capabilities"]> = {},
): AcpSessionInfo {
  return {
    capabilities: {
      resume: false,
      close: false,
      delete: false,
      ...capabilities,
    },
    additional_directories: [],
  };
}

function chipClasses(entry: WatchingSessionEntry): string[] {
  const { container } = render(renderBadges(entry));
  return Array.from(container.querySelectorAll<HTMLElement>(".chip")).map(
    (chip) => chip.className,
  );
}

describe("renderBadges kind chip", () => {
  it("renders an ACP chip in place of the web/tmux chip for ACP rows", () => {
    const chips = chipClasses(makeEntry({ acp: acpBlock() }));

    expect(chips[0]).toBe("chip chip--acp");
    expect(chips).not.toContain("chip chip--web");
    expect(chips).not.toContain("chip chip--tmux");
  });

  it("delegates to the web chip when no acp block is present", () => {
    const chips = chipClasses(makeEntry({ acp: null }));

    expect(chips[0]).toBe("chip chip--web");
    expect(chips).not.toContain("chip chip--acp");
  });

  it("delegates to the tmux chip for terminal sessions", () => {
    const chips = chipClasses(makeEntry({ sessionType: "terminal" }));

    expect(chips[0]).toBe("chip chip--tmux");
  });

  it("keeps the kind chip leading even when mode chips sort before it", () => {
    // The mode chips "auto", "LOCAL", and "SB" all sort alphabetically before
    // the "web" kind label, so a sort that swept the kind chip in would push it
    // off the front. Asserting "web" stays first proves it renders outside the
    // alphabetical sort.
    const chips = chipClasses(
      makeEntry({
        sandboxEnabled: true,
        isLocal: true,
        agentRunId: "run-1",
      }),
    );

    expect(chips[0]).toBe("chip chip--web");
    expect(chips).toContain("chip chip--auto");
    expect(chips).toContain("chip chip--local");
    expect(chips).toContain("chip chip--sandbox");
  });

  it("renders the ACP chip leading ahead of sorted mode chips", () => {
    const chips = chipClasses(
      makeEntry({
        acp: acpBlock({ resume: true }),
        sandboxEnabled: true,
        isLocal: true,
      }),
    );

    expect(chips[0]).toBe("chip chip--acp");
    // Remaining chips stay alphabetically sorted: LOCAL before SB.
    const rest = chips.slice(1);
    expect(rest).toEqual(["chip chip--local", "chip chip--sandbox"]);
  });
});

// Type-level guard: the Badge contract the chips are built from is exported and
// shaped as expected, so downstream gating helpers (task #17400) can reuse it.
const _badgeShape: Badge = { label: "ACP", className: "chip chip--acp" };
void _badgeShape;
