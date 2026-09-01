import { createRef } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { NO_CHECKOUT_MESSAGE } from "../../../lib/projectCheckout";
import type { ChatState, VoiceProps } from "../../../types/chat";
import { ChatMainColumn } from "../ChatMainColumn";
import type { MessageListHandle } from "../MessageList";
import type { UseChatPageProviderStateResult } from "../useChatPageProviderState";
import type { UseChatPageVoiceStatusResult } from "../useChatPageVoiceStatus";

vi.mock("../CommandBar", () => ({
  CommandBar: () => <div data-testid="command-bar" />,
}));
vi.mock("../MessageList", () => ({
  MessageList: () => <div data-testid="message-list" />,
}));
vi.mock("../ChatInput", () => ({
  ChatInput: () => <div data-testid="chat-input" />,
}));
vi.mock("../AgentStatusBar", () => ({
  AgentStatusBar: () => null,
}));
vi.mock("../VoiceStatusBar", () => ({
  VoiceStatusBar: () => null,
}));

function renderColumn(options: {
  projectHasCheckout?: boolean;
  checkoutRequired?: boolean;
  showChatInput?: boolean;
}) {
  const chat = {
    messages: [],
    isReconnecting: false,
    isStreaming: false,
    isThinking: false,
    checkoutRequired: options.checkoutRequired,
  } as unknown as ChatState;
  const providerState = {
    availableProviders: [],
    providerModelCatalog: {},
    viewingMeta: null,
    showChatInput: options.showChatInput ?? true,
    chatInputDisabled: false,
  } as unknown as UseChatPageProviderStateResult;
  const voiceStatus = {
    showVoiceStatusBar: false,
  } as unknown as UseChatPageVoiceStatusResult;

  render(
    <ChatMainColumn
      chat={chat}
      voice={{} as VoiceProps}
      projectId="proj-1"
      projectHasCheckout={options.projectHasCheckout}
      panelVisible={false}
      effectiveSessionRef={null}
      activeTitle={null}
      mainSessionSource={null}
      messageListRef={createRef<MessageListHandle>()}
      providerState={providerState}
      voiceStatus={voiceStatus}
      onOpenPalette={vi.fn()}
      onTogglePanel={vi.fn()}
      onPaletteSelect={vi.fn()}
      onNewChat={vi.fn()}
      agentDefinitions={[]}
      agentGlobalDefs={[]}
      agentProjectDefs={[]}
      agentShowScopeToggle={false}
      agentHasGlobal={false}
      agentHasProject={false}
    />,
  );
}

describe("ChatMainColumn checkout banner", () => {
  it("stays hidden while the selected project is checked out here", () => {
    renderColumn({ projectHasCheckout: true });

    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByTestId("chat-input")).toBeInTheDocument();
  });

  it("warns above the composer when the project has no checkout on this machine", () => {
    renderColumn({ projectHasCheckout: false });

    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent(NO_CHECKOUT_MESSAGE);
    expect(banner.querySelector("svg")).not.toBeNull();
    // Banner precedes the composer in document order.
    expect(
      banner.compareDocumentPosition(screen.getByTestId("chat-input")) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("warns when the server refused the session with checkout_required", () => {
    renderColumn({ projectHasCheckout: true, checkoutRequired: true });

    expect(screen.getByRole("status")).toHaveTextContent(NO_CHECKOUT_MESSAGE);
  });

  it("follows the composer: no input, no banner", () => {
    renderColumn({ projectHasCheckout: false, showChatInput: false });

    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByTestId("chat-input")).toBeNull();
  });
});
