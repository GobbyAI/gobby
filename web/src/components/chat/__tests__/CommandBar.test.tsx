import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { CommandBar } from "../CommandBar";

describe("CommandBar", () => {
  it("does not render attach for viewed web chat sessions", () => {
    render(
      <CommandBar
        sessionRef="#42"
        title="Viewed web chat"
        viewingMeta={{
          ref: "#42",
          source: "claude",
          title: "Viewed web chat",
          status: "paused",
          model: "sonnet",
          externalId: "web-ext",
          sessionType: "web_chat",
        }}
        isAttached={false}
        sessionInteractionMode="none"
        onAttach={undefined}
        onDetach={vi.fn()}
        onOpenPalette={vi.fn()}
        onOpenActiveSessions={vi.fn()}
        onNewChat={vi.fn()}
        onTogglePanel={vi.fn()}
        agents={[]}
        isPanelPinned={false}
      />,
    );

    expect(screen.queryByText("Attach")).toBeNull();
  });

  it("renders attach for viewed terminal sessions", () => {
    render(
      <CommandBar
        sessionRef="#43"
        title="Viewed terminal"
        viewingMeta={{
          ref: "#43",
          source: "claude",
          title: "Viewed terminal",
          status: "active",
          model: "sonnet",
          externalId: "term-ext",
          sessionType: "terminal",
        }}
        isAttached={false}
        sessionInteractionMode="observe"
        onAttach={vi.fn()}
        onDetach={vi.fn()}
        onOpenPalette={vi.fn()}
        onOpenActiveSessions={vi.fn()}
        onNewChat={vi.fn()}
        onTogglePanel={vi.fn()}
        agents={[]}
        isPanelPinned={false}
      />,
    );

    expect(screen.getByText("Attach")).toBeTruthy();
  });
});
