import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { renderBadges } from "../SessionsTab.helpers";
import type { Badge, WatchingSessionEntry } from "../SessionsTab.helpers";
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

function chipLabels(entry: WatchingSessionEntry): (string | null)[] {
  const { container } = render(renderBadges(entry));
  return Array.from(container.children).map((chip) => chip.textContent);
}

describe("renderBadges kind chip", () => {
  it("renders an ACP chip in place of the web/tmux chip for ACP rows", () => {
    const labels = chipLabels(makeEntry({ acp: acpBlock() }));

    expect(labels[0]).toBe("ACP");
    expect(labels).not.toContain("web");
    expect(labels).not.toContain("tmux");
  });

  it("delegates to the web chip when no acp block is present", () => {
    const labels = chipLabels(makeEntry({ acp: null }));

    expect(labels[0]).toBe("web");
    expect(labels).not.toContain("ACP");
  });

  it("delegates to the tmux chip for terminal sessions", () => {
    const labels = chipLabels(makeEntry({ sessionType: "terminal" }));

    expect(labels[0]).toBe("tmux");
  });

  it("renders every kind and mode chip through the identity Chip treatment", () => {
    const { container } = render(
      renderBadges(makeEntry({ sandboxEnabled: true })),
    );

    for (const chip of Array.from(container.children)) {
      expect(chip).toHaveClass("rounded-full", "uppercase", "font-mono");
    }
  });

  it("keeps the kind chip leading even when mode chips sort before it", () => {
    // The mode chips "auto", "LOCAL", and "SB" all sort alphabetically before
    // the "web" kind label, so a sort that swept the kind chip in would push it
    // off the front. Asserting "web" stays first proves it renders outside the
    // alphabetical sort.
    const labels = chipLabels(
      makeEntry({
        sandboxEnabled: true,
        isLocal: true,
        agentRunId: "run-1",
      }),
    );

    expect(labels).toEqual(["web", "auto", "LOCAL", "SB"]);
  });

  it("renders the ACP chip leading ahead of sorted mode chips", () => {
    const labels = chipLabels(
      makeEntry({
        acp: acpBlock({ resume: true }),
        sandboxEnabled: true,
        isLocal: true,
      }),
    );

    expect(labels[0]).toBe("ACP");
    // Remaining chips stay alphabetically sorted: LOCAL before SB.
    expect(labels.slice(1)).toEqual(["LOCAL", "SB"]);
  });
});

// Type-level guard: the Badge contract the chips are built from is exported and
// shaped as expected, so downstream gating helpers (task #17400) can reuse it.
const _badgeShape: Badge = { label: "ACP" };
void _badgeShape;
