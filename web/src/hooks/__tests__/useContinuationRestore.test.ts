import { act, renderHook } from "@testing-library/react";
import type { MutableRefObject } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ChatMessage, ContentBlock } from "../../types/chat";
import { isContentBlock, useContinuationRestore } from "../useChat/useContinuationRestore";
import type { ContinuationRollbackSnapshot } from "../useChat/sessionRecords";

function ref<T>(current: T): MutableRefObject<T> {
  return { current };
}

const contentBlockSamples = {
  text: { type: "text", content: "Text block" },
  thinking: { type: "thinking", content: "Thinking block" },
  compaction_summary: { type: "compaction_summary", content: "Conversation compacted (manual)" },
  tool_chain: {
    type: "tool_chain",
    tool_calls: [
      {
        id: "tool-1",
        tool_name: "read_file",
        server_name: "builtin",
        tool_type: "read",
        status: "completed",
      },
    ],
  },
  tool_reference: {
    type: "tool_reference",
    tool_name: "read_file",
    server_name: "builtin",
  },
  attachment: {
    type: "attachment",
    attachment: {
      id: "att-1",
      project_id: "proj-1",
      filename: "notes.txt",
      mime_type: "text/plain",
      size_bytes: 10,
      content_url: "/api/chat/attachments/att-1/content",
    },
  },
  image: {
    type: "image",
    image_url: { url: "https://example.test/image.png" },
  },
  document: {
    type: "document",
    source: { name: "contract.pdf" },
  },
  web_search_result: {
    type: "web_search_result",
    content: { title: "Result" },
  },
  resource_link: {
    type: "resource_link",
    uri: "file:///repo/src/app.ts",
    name: "src/app.ts",
  },
  resource: {
    type: "resource",
    resource: { name: "Resource", text: "body" },
  },
  audio: {
    type: "audio",
    data: "AAAA",
    mime_type: "audio/wav",
  },
  diff: {
    type: "diff",
    path: "src/app.ts",
    old_text: "old",
    new_text: "new",
  },
  terminal: {
    type: "terminal",
    terminal_id: "term-1",
  },
  unknown: {
    type: "unknown",
    block_type: "future_block",
    raw: { future: true },
    source_line: 7,
  },
} satisfies Record<ContentBlock["type"], ContentBlock>;

function makeSnapshot(messages: ChatMessage[]): ContinuationRollbackSnapshot {
  return {
    sourceSessionId: "source-session",
    conversationId: "conversation-1",
    dbSessionId: "db-session-1",
    mainSessionMeta: null,
    sessionTitle: "Restored",
    sessionRef: "#1",
    selectedProvider: "codex",
    messages,
    contextUsage: {
      totalInputTokens: 0,
      outputTokens: 0,
      contextWindow: null,
      uncachedInputTokens: 0,
      cacheReadTokens: 0,
      cacheCreationTokens: 0,
    },
    currentMode: "normal",
    currentBranch: null,
    worktreePath: null,
    viewingSessionId: null,
    viewingSessionMeta: null,
    observedSessionId: null,
    observedSessionMeta: null,
    attachedSessionId: null,
    attachedSessionMeta: null,
    sessionInteractionMode: "none",
    proxyDeliveryNotice: null,
  };
}

function renderRestoreHook(setMessages = vi.fn()) {
  return renderHook(() =>
    useContinuationRestore({
      sessionRefs: {
        attachedSessionIdRef: ref<string | null>(null),
        conversationIdRef: ref(""),
        dbSessionIdRef: ref<string | null>(null),
        observedSessionIdRef: ref<string | null>(null),
        viewingSessionIdRef: ref<string | null>(null),
      },
      sessionSetters: {
        setAttachedSessionId: vi.fn(),
        setConversationId: vi.fn(),
        setDbSessionId: vi.fn(),
        setObservedSessionId: vi.fn(),
        setSelectedProvider: vi.fn(),
        setSessionRef: vi.fn(),
        setSessionTitle: vi.fn(),
        setViewingSessionId: vi.fn(),
      },
      conversationRefs: {
        attachedSessionMetaRef: ref(null),
        observedSessionMetaRef: ref(null),
        viewingSessionMetaRef: ref(null),
        wsRef: ref<WebSocket | null>(null),
      },
      conversationSetters: {
        setAttachedSessionMeta: vi.fn(),
        setContextUsage: vi.fn(),
        setCurrentBranch: vi.fn(),
        setCurrentMode: vi.fn(),
        setIsLoadingMessages: vi.fn(),
        setMainSessionMeta: vi.fn(),
        setMessages,
        setProxyDeliveryNotice: vi.fn(),
        setViewingSessionMeta: vi.fn(),
        setWorktreePath: vi.fn(),
      },
      interactionMode: {
        pendingSessionInteractionModeRef: ref<"observe" | "proxy">("observe"),
        sessionInteractionModeRef: ref<ContinuationRollbackSnapshot["sessionInteractionMode"]>(
          "none",
        ),
        setSessionInteractionMode: vi.fn(),
      },
    }),
  );
}

describe("useContinuationRestore content block normalization", () => {
  it("accepts every ContentBlock variant", () => {
    expect(Object.keys(contentBlockSamples)).toHaveLength(15);
    for (const block of Object.values(contentBlockSamples)) {
      expect(isContentBlock(block)).toBe(true);
    }
  });

  it("preserves restored document, diff, compaction_summary, and unknown content blocks", () => {
    const setMessages = vi.fn();
    const blocks = [
      contentBlockSamples.document,
      contentBlockSamples.diff,
      contentBlockSamples.compaction_summary,
      contentBlockSamples.unknown,
    ];
    const snapshot = makeSnapshot([
      {
        id: "message-1",
        role: "assistant",
        content: "",
        timestamp: new Date("2026-06-27T12:00:00Z"),
        contentBlocks: blocks,
      },
    ]);
    const { result } = renderRestoreHook(setMessages);

    act(() => result.current(snapshot));

    expect(setMessages).toHaveBeenCalledWith([
      expect.objectContaining({
        id: "message-1",
        contentBlocks: blocks,
      }),
    ]);
  });
});
