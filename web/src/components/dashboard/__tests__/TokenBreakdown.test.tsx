import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ModelBreakdownList } from "../ModelBreakdownList";
import { ModelDistributionBar } from "../ModelDistributionBar";
import type { ModelBreakdown } from "../../../types/tokens";

const SAMPLE_ITEMS: ModelBreakdown[] = [
  {
    family: "claude 4",
    inputTokens: 70,
    outputTokens: 30,
    cacheReadTokens: 10,
    cacheCreationTokens: 5,
    sessionCount: 2,
    totalTokens: 100,
    percentage: 50,
    models: [
      {
        model: "claude-sonnet-4",
        inputTokens: 50,
        outputTokens: 20,
        cacheReadTokens: 5,
        cacheCreationTokens: 3,
        sessionCount: 1,
        totalTokens: 70,
      },
    ],
  },
  {
    family: "codex",
    inputTokens: 60,
    outputTokens: 40,
    cacheReadTokens: 0,
    cacheCreationTokens: 0,
    sessionCount: 1,
    totalTokens: 100,
    percentage: 50,
    models: [],
  },
];

describe("Token breakdown widgets", () => {
  it("wires the breakdown toggle to an accessible region", () => {
    render(<ModelBreakdownList items={SAMPLE_ITEMS} />);

    const toggle = screen.getByRole("button", { name: /claude 4/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    const panelId = toggle.getAttribute("aria-controls");
    expect(panelId).toBeTruthy();

    const panel = document.getElementById(panelId!);
    expect(panel).toHaveAttribute("role", "region");
    expect(panel).toHaveAttribute("aria-labelledby", toggle.id);
    expect(panel).toHaveAttribute("aria-hidden", "true");

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(panel).toHaveAttribute("aria-hidden", "false");
    expect(panel).not.toHaveAttribute("hidden");
    expect(screen.getByText("claude-sonnet-4")).toBeInTheDocument();
  });

  it("keeps the distribution bar at or below 100 percent total width", () => {
    const manyItems: ModelBreakdown[] = Array.from({ length: 60 }, (_, index) => ({
      family: `family-${index}`,
      inputTokens: 1,
      outputTokens: 0,
      cacheReadTokens: 0,
      cacheCreationTokens: 0,
      sessionCount: 1,
      totalTokens: 1,
      percentage: 100 / 60,
      models: [],
    }));

    const { container } = render(<ModelDistributionBar items={manyItems} />);

    const segments = Array.from(container.querySelectorAll("[title]"));
    const totalWidth = segments.reduce((sum, segment) => {
      return sum + Number.parseFloat((segment as HTMLElement).style.width || "0");
    }, 0);

    expect(totalWidth).toBeLessThanOrEqual(100.01);
  });
});
