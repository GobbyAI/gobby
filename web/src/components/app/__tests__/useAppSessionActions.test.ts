import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { useAppSessionActions } from "../useAppSessionActions";
import {
  createMockFetch,
  type MockFetchInstance,
} from "../../../test/mocks/fetch";
import type {
  ContinueSessionInChatAction,
  DeleteConversationAction,
  SwitchConversationAction,
} from "../../../hooks/useChat/actionTypes";

type Args = Parameters<typeof useAppSessionActions>[0];

function makeArgs(overrides: Partial<Args> = {}): Args {
  return {
    attachedSessionId: null,
    clearViewingSession: vi.fn(),
    confirmSessionDeleted: vi.fn(),
    continueSessionInChat: vi.fn<ContinueSessionInChatAction>(),
    deleteConversation: vi.fn<DeleteConversationAction>(),
    detachFromSession: vi.fn(),
    markSessionDeleting: vi.fn(),
    restoreSession: vi.fn(),
    setActiveTab: vi.fn(),
    setOnChatDeleted: vi.fn(),
    showToast: vi.fn(),
    switchConversation: vi.fn<SwitchConversationAction>(),
    viewingSessionId: null,
    ...overrides,
  };
}

describe("useAppSessionActions — ACP close/delete (#17400)", () => {
  let mockFetch: MockFetchInstance;

  beforeEach(() => {
    mockFetch = createMockFetch();
  });

  afterEach(() => {
    mockFetch.restore();
    vi.restoreAllMocks();
  });

  it("POSTs to /acp/close and returns true on success", async () => {
    mockFetch.mockJsonResponse("/acp/close", { session: { id: "s1" } });
    const showToast = vi.fn();
    const { result } = renderHook(() =>
      useAppSessionActions(makeArgs({ showToast })),
    );

    let outcome: boolean | void = false;
    await act(async () => {
      outcome = await result.current.handleCloseSession("s1");
    });

    expect(outcome).toBe(true);
    expect(mockFetch.fn).toHaveBeenCalledWith(
      "/api/sessions/s1/acp/close",
      { method: "POST" },
    );
    expect(showToast).not.toHaveBeenCalled();
  });

  it("returns false and toasts when /acp/close fails", async () => {
    mockFetch.mockErrorResponse("/acp/close", 409);
    const showToast = vi.fn();
    const { result } = renderHook(() =>
      useAppSessionActions(makeArgs({ showToast })),
    );

    let outcome: boolean | void = true;
    await act(async () => {
      outcome = await result.current.handleCloseSession("s1");
    });

    expect(outcome).toBe(false);
    expect(showToast).toHaveBeenCalledWith("Failed to close session");
  });

  it("marks the row deleting and POSTs to /acp/delete on success", async () => {
    mockFetch.mockJsonResponse("/acp/delete", { session: { id: "s1" } });
    const markSessionDeleting = vi.fn();
    const restoreSession = vi.fn();
    const { result } = renderHook(() =>
      useAppSessionActions(makeArgs({ markSessionDeleting, restoreSession })),
    );

    let outcome: boolean | void = false;
    await act(async () => {
      outcome = await result.current.handleDeleteSession("s1");
    });

    expect(outcome).toBe(true);
    expect(markSessionDeleting).toHaveBeenCalledWith("s1");
    expect(mockFetch.fn).toHaveBeenCalledWith(
      "/api/sessions/s1/acp/delete",
      { method: "POST" },
    );
    // Success leaves the optimistic removal in place; the session_deleted WS
    // event finalizes it, so the row is never restored.
    expect(restoreSession).not.toHaveBeenCalled();
  });

  it("restores the row and toasts when /acp/delete fails", async () => {
    mockFetch.mockErrorResponse("/acp/delete", 500);
    const markSessionDeleting = vi.fn();
    const restoreSession = vi.fn();
    const showToast = vi.fn();
    const { result } = renderHook(() =>
      useAppSessionActions(
        makeArgs({ markSessionDeleting, restoreSession, showToast }),
      ),
    );

    let outcome: boolean | void = true;
    await act(async () => {
      outcome = await result.current.handleDeleteSession("s1");
    });

    expect(outcome).toBe(false);
    expect(markSessionDeleting).toHaveBeenCalledWith("s1");
    expect(restoreSession).toHaveBeenCalledWith("s1");
    expect(showToast).toHaveBeenCalledWith("Failed to delete session");
  });
});
