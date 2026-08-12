import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { Plan } from "../../../types/plans";
import { PlanReviewCard } from "../../activity/PlanReviewCard";
import { PlanPendingActionStrip } from "../PlanPendingActionStrip";
import { getPlanPendingColors } from "../planPendingSurface";
import { AgentStatusBar } from "../AgentStatusBar";

const planPendingColors = getPlanPendingColors("info");
const commandBarSource = readFileSync(
  resolve("src/components/chat/CommandBar.tsx"),
  "utf8",
);
const activityPanelSource = readFileSync(
  resolve("src/components/activity/ActivityPanel.tsx"),
  "utf8",
);

// Render markdown as plain text so the pending banner is the only thing under test.
vi.mock("../Markdown", () => ({
  Markdown: ({ content }: { content: string }) => <div>{content}</div>,
}));

function makePlan(): Plan {
  return {
    id: "plan-1",
    title: "Plan",
    versions: [{ content: "# Plan", timestamp: new Date(1_700_000_000_000) }],
    currentVersionIndex: 0,
  };
}

/**
 * Design-fix guards for #15637 / #15693. The awaiting-approval surface shares a
 * single swappable color treatment (`getPlanPendingColors`) across the Plans panel
 * header and the status-bar strip, so these assert against that treatment and
 * track the active variant automatically. They still pin the regressions this
 * epic cared about (.impeccable.md: state read by lightness/icon first, never
 * hue alone; tokens, never hardcoded colors):
 *  - the awaiting-approval bar fills with the shared SURFACE token, never the
 *    *-foreground* token misused as a fill (the original muddy-brown bug);
 *  - the foreground token still carries the icon + label (grayscale-legible);
 *  - the status bars stay pinned to --activity-panel-bar-height with no inner
 *    redeclaration, and in-bar plan controls to --status-bar-control-height.
 */
describe("plan-approval design fixes (#15637)", () => {
  it("PlanReviewCard pending bar fills with the shared SURFACE token, not foreground", () => {
    render(
      <PlanReviewCard
        plan={makePlan()}
        planPendingApproval
        onSetVersion={vi.fn()}
      />,
    );
    const banner = screen.getByTestId("plan-review-status");
    expect(banner.getAttribute("data-status")).toBe("pending");
    // Background is the shared surface fill for the active treatment...
    expect(banner.className).toContain(planPendingColors.surfaceBg);
    // ...never a *-foreground/text token misused as a fill (the brown bug).
    expect(banner.className).not.toContain("var(--color-warning-foreground)");
  });

  it("PlanReviewCard carries the icon + label with the shared accent token", () => {
    render(
      <PlanReviewCard
        plan={makePlan()}
        planPendingApproval
        onSetVersion={vi.fn()}
      />,
    );
    const banner = screen.getByTestId("plan-review-status");
    // The state hue carries the icon/label (grayscale-legible: an icon plus the
    // label, never hue alone).
    expect(banner.querySelector("svg")).toBeTruthy();
    expect(banner.innerHTML).toContain(planPendingColors.accentText);
  });

  it("PlanPendingActionStrip fills with the shared SURFACE token, not foreground", () => {
    render(
      <PlanPendingActionStrip
        onApprove={vi.fn()}
        onRequestChanges={vi.fn()}
        onView={vi.fn()}
      />,
    );
    const strip = screen.getByTestId("plan-pending-strip");
    expect(strip.className).toContain(planPendingColors.surfaceBg);
    expect(strip.className).not.toContain("var(--color-warning-foreground)");
  });

  it("PlanReviewCard uses the runtime amber variant when requested", () => {
    render(
      <PlanReviewCard
        plan={makePlan()}
        planPendingApproval
        onSetVersion={vi.fn()}
        planPendingVariant="amber"
      />,
    );
    const banner = screen.getByTestId("plan-review-status");
    const amber = getPlanPendingColors("amber");
    expect(banner.className).toContain(amber.surfaceBg);
    expect(banner.innerHTML).toContain(amber.accentText);
  });

  it("AgentStatusBar owns the shared activity-bar height through component utilities", () => {
    render(<AgentStatusBar interactionMode="none" />);
    expect(screen.getByTestId("agent-status-bar").className).toContain(
      "min-h-[var(--activity-panel-bar-height,2.5rem)]",
    );
  });

  it("CommandBar owns the shared activity-bar height through component utilities", () => {
    expect(commandBarSource).toContain(
      "min-h-[var(--activity-panel-bar-height)]",
    );
  });

  it("ActivityPanel owns the shared tab-bar height and padding through component utilities", () => {
    expect(activityPanelSource).toContain("activity-panel-tabs");
    expect(activityPanelSource).toContain(
      "min-h-[var(--activity-panel-bar-height)]",
    );
    expect(activityPanelSource).toContain("px-3");
  });

  it("in-bar plan controls are pinned to --status-bar-control-height so they cannot stretch the bar", () => {
    render(
      <AgentStatusBar
        interactionMode="none"
        planPendingApproval
        onApprovePlan={vi.fn()}
        onRequestPlanChanges={vi.fn()}
      />,
    );
    const plan = screen.getByTestId("plan-pending-strip").parentElement;
    expect(plan?.className).toContain(
      "[&_button]:h-[var(--status-bar-control-height)]",
    );
    expect(plan?.className).toContain(
      "[&_button]:min-h-[var(--status-bar-control-height)]",
    );
  });
});
