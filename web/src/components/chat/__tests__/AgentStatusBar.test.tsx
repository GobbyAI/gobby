import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AgentStatusBar } from "../AgentStatusBar";

describe("AgentStatusBar", () => {
  it("renders only the lower-bar state and transport chip", () => {
    render(
      <AgentStatusBar
        viewingMeta={{
          ref: "#77",
          source: "codex",
          title: "Observed Session",
          status: "active",
          model: "gpt-5.4",
          externalId: "ext-77",
          chatMode: "plan",
          gitBranch: null,
          contextWindow: 200000,
          agentRunId: null,
          workflowName: null,
          agentName: "triage-agent",
          sessionType: "terminal",
        }}
        interactionMode="observe"
      />,
    );

    expect(screen.getByText("Watching live")).toBeInTheDocument();
    expect(screen.getByText("TMUX")).toBeInTheDocument();
    expect(screen.queryByText("#77")).toBeNull();
    expect(screen.queryByText("Observed Session")).toBeNull();
    expect(screen.queryByText("gpt-5.4")).toBeNull();
    expect(screen.queryByText("triage-agent")).toBeNull();
  });

  it("uses the shared neutral session action styling for resume", async () => {
    const onAttach = vi.fn();
    const onResume = vi.fn();
    const onDetach = vi.fn();

    render(
      <AgentStatusBar
        viewingMeta={{
          ref: "#88",
          source: "claude",
          title: "Observed Session",
          status: "paused",
          model: "sonnet",
          externalId: "ext-88",
          chatMode: "normal",
          gitBranch: null,
          contextWindow: null,
          agentRunId: null,
          workflowName: null,
          agentName: null,
          sessionType: "terminal",
        }}
        interactionMode="observe"
        onAttach={onAttach}
        onResume={onResume}
        onDetach={onDetach}
      />,
    );

    const attachButton = screen.getByRole("button", { name: "Attach" });
    const resumeButton = screen.getByRole("button", { name: "Resume" });
    // Tinted-accent Button, never the solid primary slab.
    expect(attachButton).toHaveClass("bg-accent-tint");
    expect(attachButton).not.toHaveClass("bg-accent");
    expect(resumeButton).toHaveClass("bg-accent-tint");
    expect(resumeButton).not.toHaveClass("bg-accent");
    expect(screen.queryByText("#88")).toBeNull();
    expect(screen.queryByText("Observed Session")).toBeNull();

    await userEvent.click(attachButton);
    await userEvent.click(resumeButton);

    expect(onAttach).toHaveBeenCalledTimes(1);
    expect(onResume).toHaveBeenCalledTimes(1);
    expect(onDetach).not.toHaveBeenCalled();
  });

  it("hides the session badge for null session types while keeping the state text", () => {
    render(
      <AgentStatusBar
        viewingMeta={{
          ref: "#91",
          source: "claude",
          title: "Observed Session",
          status: "active",
          model: "sonnet",
          externalId: "ext-91",
          chatMode: "normal",
          gitBranch: null,
          contextWindow: null,
          agentRunId: null,
          workflowName: null,
          agentName: null,
          sessionType: null,
        }}
        interactionMode="observe"
      />,
    );

    expect(screen.getByText("Watching live")).toBeInTheDocument();
    expect(screen.queryByText("TMUX")).toBeNull();
    expect(screen.queryByText("WEB")).toBeNull();
  });

  it("renders the New Chat button by default and invokes onNewChat when clicked", async () => {
    const onNewChat = vi.fn();

    render(<AgentStatusBar interactionMode="none" onNewChat={onNewChat} />);

    const newChatButton = screen.getByRole("button", { name: /new chat/i });
    expect(newChatButton).toBeInTheDocument();
    expect(newChatButton).toBeEnabled();
    expect(newChatButton).toHaveClass("bg-accent-tint", "chat-new-chat-btn");

    await userEvent.click(newChatButton);
    expect(onNewChat).toHaveBeenCalledTimes(1);
  });

  it("disables the New Chat button when onNewChat is not provided", () => {
    render(<AgentStatusBar interactionMode="none" />);

    const newChatButton = screen.getByRole("button", { name: /new chat/i });
    expect(newChatButton).toBeDisabled();
  });

  it("terminal button", async () => {
    const onOpenTerminal = vi.fn();
    const { rerender } = render(<AgentStatusBar interactionMode="none" />);

    expect(screen.queryByRole("button", { name: "Terminal" })).toBeNull();

    rerender(
      <AgentStatusBar interactionMode="none" onOpenTerminal={onOpenTerminal} />,
    );

    const terminalButton = screen.getByRole("button", { name: "Terminal" });
    expect(terminalButton).toHaveClass("bg-accent-tint");
    expect(terminalButton.querySelector("polyline")).toHaveAttribute(
      "points",
      "4 17 10 11 4 5",
    );
    expect(terminalButton.querySelector("line")).toHaveAttribute("x1", "12");

    await userEvent.click(terminalButton);

    expect(onOpenTerminal).toHaveBeenCalledTimes(1);
  });

  it("shows Resume and Detach (but not Attach) while attached", () => {
    const onResume = vi.fn();
    const onDetach = vi.fn();

    render(
      <AgentStatusBar
        viewingMeta={{
          ref: "#89",
          source: "claude",
          title: "Attached Session",
          status: "active",
          model: "sonnet",
          externalId: "ext-89",
          chatMode: "normal",
          gitBranch: null,
          contextWindow: null,
          agentRunId: null,
          workflowName: null,
          agentName: null,
          sessionType: "terminal",
        }}
        interactionMode="proxy"
        isAttached={true}
        onAttach={vi.fn()}
        onResume={onResume}
        onDetach={onDetach}
      />,
    );

    expect(screen.queryByRole("button", { name: "Attach" })).toBeNull();
    expect(screen.getByText("Attached")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Detach" })).toBeInTheDocument();
  });

  it("keeps context usage visible while attached", () => {
    render(
      <AgentStatusBar
        viewingMeta={{
          ref: "#89",
          source: "claude",
          title: "Attached Session",
          status: "active",
          model: "sonnet",
          externalId: "ext-89",
          chatMode: "normal",
          gitBranch: null,
          contextWindow: 200000,
          agentRunId: null,
          workflowName: null,
          agentName: null,
          sessionType: "terminal",
        }}
        interactionMode="proxy"
        isAttached={true}
        contextUsage={{
          totalInputTokens: 100000,
          outputTokens: 1200,
          contextWindow: 200000,
          uncachedInputTokens: 90000,
          cacheReadTokens: 8000,
          cacheCreationTokens: 2000,
        }}
      />,
    );

    expect(screen.getByTestId("context-usage-indicator")).toHaveTextContent(
      "50%",
    );
  });

  it("hides Resume while attached if the session is autonomous", () => {
    render(
      <AgentStatusBar
        viewingMeta={{
          ref: "#90",
          source: "claude",
          title: "Autonomous Agent Session",
          status: "active",
          model: "sonnet",
          externalId: "ext-90",
          chatMode: "normal",
          gitBranch: null,
          contextWindow: null,
          agentRunId: "run-1",
          workflowName: null,
          agentName: null,
          sessionType: "terminal",
        }}
        interactionMode="proxy"
        isAttached={true}
        isAutonomousSession={true}
        onResume={vi.fn()}
        onDetach={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    expect(screen.getByRole("button", { name: "Detach" })).toBeInTheDocument();
  });

  it("shows Attach for a watched autonomous terminal session", () => {
    render(
      <AgentStatusBar
        viewingMeta={{
          ref: "#91",
          source: "claude",
          title: "Autonomous Agent Session",
          status: "active",
          model: "sonnet",
          externalId: "ext-91",
          chatMode: "normal",
          gitBranch: null,
          contextWindow: null,
          agentRunId: "run-1",
          workflowName: null,
          agentName: null,
          sessionType: "terminal",
        }}
        interactionMode="observe"
        isAttached={false}
        isAutonomousSession={true}
        onAttach={vi.fn()}
        onResume={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Attach" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
  });
});
