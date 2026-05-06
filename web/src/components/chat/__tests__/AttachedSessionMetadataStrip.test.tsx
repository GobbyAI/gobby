import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { AttachedSessionMetadataStrip } from "../AttachedSessionMetadataStrip";
import type { SessionObservationMeta } from "../../../types/chat";

function makeMeta(overrides: Partial<SessionObservationMeta> = {}): SessionObservationMeta {
  return {
    ref: "#42",
    source: "claude",
    title: "test session",
    status: "active",
    model: "claude-haiku-4-5",
    reasoningEffort: "auto",
    externalId: "ext-42",
    chatMode: "plan",
    gitBranch: "main",
    contextWindow: 200_000,
    sessionType: "terminal",
    ...overrides,
  };
}

describe("AttachedSessionMetadataStrip", () => {
  it("renders only Model and Branch (Provider, Reasoning, Window are hidden)", () => {
    render(<AttachedSessionMetadataStrip meta={makeMeta()} />);

    expect(screen.getByText("Model")).toBeInTheDocument();
    expect(screen.getByText("claude-haiku-4-5")).toBeInTheDocument();
    expect(screen.getByText("Branch")).toBeInTheDocument();
    expect(screen.getByText("main")).toBeInTheDocument();

    // Provider lives in the AgentStatusBar's TMUX/WEB chip; reasoning and
    // chat_mode are not reliably tracked for tmux sessions today; window
    // is not user-relevant for the attached-mode strip.
    expect(screen.queryByText("Provider")).toBeNull();
    expect(screen.queryByText("Reasoning")).toBeNull();
    expect(screen.queryByText("Window")).toBeNull();
  });

  it("renders an em dash for null values without showing 'null'", () => {
    render(
      <AttachedSessionMetadataStrip
        meta={makeMeta({ model: null, gitBranch: null })}
      />,
    );

    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText("null")).toBeNull();
  });
});
