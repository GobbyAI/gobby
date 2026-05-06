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
  it("renders Provider · Model · Reasoning · Branch · Window from meta", () => {
    const meta = makeMeta();
    render(<AttachedSessionMetadataStrip meta={meta} />);

    expect(screen.getByText("Provider")).toBeInTheDocument();
    expect(screen.getByText("Claude")).toBeInTheDocument();

    expect(screen.getByText("Model")).toBeInTheDocument();
    expect(screen.getByText("claude-haiku-4-5")).toBeInTheDocument();

    expect(screen.getByText("Reasoning")).toBeInTheDocument();
    expect(screen.getByText("Auto")).toBeInTheDocument();

    expect(screen.getByText("Branch")).toBeInTheDocument();
    expect(screen.getByText("main")).toBeInTheDocument();

    expect(screen.getByText("Window")).toBeInTheDocument();
    expect(screen.getByText("200k")).toBeInTheDocument();
  });

  it("uses Droid as the Provider label even when the session runs an Anthropic model", () => {
    // Droid broadcasts multiple vendors' models; provider must come from
    // `source` (the broadcaster), not be derived from the model name.
    const meta = makeMeta({ source: "droid", model: "claude-sonnet-4-5" });
    render(<AttachedSessionMetadataStrip meta={meta} />);

    expect(screen.getByText("Droid")).toBeInTheDocument();
    expect(screen.queryByText("Claude")).toBeNull();
    expect(screen.getByText("claude-sonnet-4-5")).toBeInTheDocument();
  });

  it("renders an em dash for null values without showing 'null'", () => {
    const meta = makeMeta({
      reasoningEffort: null,
      gitBranch: null,
      contextWindow: null,
    });
    render(<AttachedSessionMetadataStrip meta={meta} />);

    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBeGreaterThanOrEqual(3);
    expect(screen.queryByText("null")).toBeNull();
  });

  it("falls back to em dash for Provider when source is 'unknown'", () => {
    const meta = makeMeta({ source: "unknown" });
    render(<AttachedSessionMetadataStrip meta={meta} />);

    const providerLabel = screen.getByText("Provider");
    const providerCell = providerLabel.parentElement!;
    expect(providerCell.textContent).toContain("—");
  });

  it("formats the context window as Nk and N.Nk", () => {
    const { rerender } = render(
      <AttachedSessionMetadataStrip meta={makeMeta({ contextWindow: 128_000 })} />,
    );
    expect(screen.getByText("128k")).toBeInTheDocument();

    rerender(
      <AttachedSessionMetadataStrip meta={makeMeta({ contextWindow: 1_500 })} />,
    );
    expect(screen.getByText("1.5k")).toBeInTheDocument();
  });
});
