import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { McpDetailPanel } from "../McpDetailPanel";

describe("McpDetailPanel", () => {
  it("blocks tool calls while a JSON argument is invalid", () => {
    const onCallTool = vi.fn();
    render(
      <McpDetailPanel
        selection={{ kind: "tool", serverName: "demo", toolName: "run" }}
        server={null}
        tool={{ name: "run", brief: "Run demo" }}
        schema={{
          name: "run",
          inputSchema: { properties: { payload: { type: "object" } } },
        }}
        schemaLoading={false}
        argumentValues={{}}
        onArgumentValuesChange={vi.fn()}
        executing={false}
        executionResult={null}
        onCallTool={onCallTool}
        status={null}
        toolsByServer={{}}
      />,
    );

    const callButton = screen.getByRole("button", { name: "Call tool" });
    const title = screen.getByText("demo.run");
    expect(title).toHaveClass(
      "truncate",
      "text-[length:var(--text-base)]",
      "font-[var(--font-weight-medium)]",
    );
    expect(title.parentElement).toHaveClass(
      "min-h-[var(--activity-panel-bar-height)]",
      "justify-between",
    );
    expect(callButton).not.toHaveClass("activity-panel-action-btn");
    // Marker class only — the panel root's shared descendant rules own the
    // mobile-tier / narrow-panel label collapse (#19187).
    expect(callButton.querySelector("span")).toHaveClass(
      "activity-panel-action-btn__label",
    );
    expect(callButton.querySelector("span")).not.toHaveClass(
      "@max-[479px]/activity-panel:hidden",
    );
    fireEvent.change(screen.getByRole("textbox", { name: "payload" }), {
      target: { value: "{" },
    });

    expect(callButton).toBeDisabled();
    fireEvent.click(callButton);
    expect(onCallTool).not.toHaveBeenCalled();
  });
});
