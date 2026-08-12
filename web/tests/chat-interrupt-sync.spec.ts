import { test, expect } from "@playwright/test";

/**
 * Tests for chat interrupt synchronization.
 *
 * When a user sends a new message while the previous response is still
 * streaming, the new message should interrupt the old stream and the
 * response to the new message should be correctly associated — NOT the
 * leftover response from the interrupted query.
 *
 * Bug: responses were off-by-one after interrupts — each response answered
 * the PREVIOUS message instead of the current one.
 *
 * Strategy: We override the WebSocket constructor so all /ws connections
 * (chat, terminal, tmux) share a mocked transport. We broadcast server
 * messages to ALL connected mocks so the chat handler receives them.
 */

const CONVERSATION_ID = "test-interrupt-conv";
const STORAGE_KEY = `gobby-chat-${CONVERSATION_ID}`;
const CONVERSATION_ID_KEY = "gobby-conversation-id";

function setupMockWebSocket(page: import("@playwright/test").Page) {
  return page.addInitScript(
    ({ convId, storageKey, convIdKey }) => {
      // --- localStorage setup ---
      localStorage.setItem(convIdKey, convId);
      localStorage.setItem(storageKey, "[]");

      // --- WebSocket mock ---
      (window as any).__sentMessages = [] as string[];
      (window as any).__mockWsReady = false;
      // Track ALL mock WebSocket instances so we can broadcast to all
      (window as any).__allMockWs = [] as any[];
      // The chat WebSocket (identified by its subscribe message)
      (window as any).__chatWs = null as any;

      const OriginalWebSocket = window.WebSocket;

      (window as any).WebSocket = function (url: string, ...rest: any[]) {
        if (
          typeof url === "string" &&
          url.includes("/ws") &&
          !url.includes("vite")
        ) {
          let _onmessage: ((ev: { data: string }) => void) | null = null;
          let _onopen: (() => void) | null = null;
          let _onclose: (() => void) | null = null;

          const mockWs = {
            readyState: 1,
            url,
            _isChat: false,
            send(data: string) {
              (window as any).__sentMessages.push(data);
              // Auto-detect the chat WebSocket by its subscribe message
              try {
                const parsed = JSON.parse(data);
                if (
                  parsed.type === "subscribe" &&
                  parsed.events?.includes("chat_stream")
                ) {
                  mockWs._isChat = true;
                  (window as any).__chatWs = mockWs;
                }
              } catch {}
            },
            close() {
              mockWs.readyState = 3;
              if (_onclose) _onclose();
            },
            addEventListener() {},
            removeEventListener() {},
            set onmessage(cb: ((ev: { data: string }) => void) | null) {
              _onmessage = cb;
            },
            get onmessage() {
              return _onmessage;
            },
            set onopen(cb: (() => void) | null) {
              _onopen = cb;
            },
            get onopen() {
              return _onopen;
            },
            set onerror(_: unknown) {},
            get onerror() {
              return null;
            },
            set onclose(cb: (() => void) | null) {
              _onclose = cb;
            },
            get onclose() {
              return _onclose;
            },
          };

          (window as any).__allMockWs.push(mockWs);
          (window as any).__mockWsReady = true;

          setTimeout(() => {
            if (mockWs.onopen) mockWs.onopen();
          }, 50);
          return mockWs;
        }
        return new OriginalWebSocket(url, ...rest);
      } as any;

      Object.defineProperty((window as any).WebSocket, "OPEN", { value: 1 });
      Object.defineProperty((window as any).WebSocket, "CLOSED", { value: 3 });
      Object.defineProperty((window as any).WebSocket, "CONNECTING", {
        value: 0,
      });
      Object.defineProperty((window as any).WebSocket, "CLOSING", { value: 2 });
    },
    {
      convId: CONVERSATION_ID,
      storageKey: STORAGE_KEY,
      convIdKey: CONVERSATION_ID_KEY,
    },
  );
}

/** Send a server message to the chat WebSocket only. */
async function serverSend(
  page: import("@playwright/test").Page,
  msg: Record<string, unknown>,
) {
  await page.evaluate((data) => {
    // For chat-specific messages, send only to the chat WS
    const chatWs = (window as any).__chatWs;
    if (chatWs?.onmessage) {
      chatWs.onmessage({ data: JSON.stringify(data) });
      return;
    }
    // Fallback: broadcast to all (for handshake before chat WS is identified)
    for (const ws of (window as any).__allMockWs || []) {
      if (ws.onmessage) {
        ws.onmessage({ data: JSON.stringify(data) });
      }
    }
  }, msg);
}

/** Broadcast a server message to ALL mocked WebSocket connections (for handshake). */
async function broadcastSend(
  page: import("@playwright/test").Page,
  msg: Record<string, unknown>,
) {
  await page.evaluate((data) => {
    for (const ws of (window as any).__allMockWs || []) {
      if (ws.onmessage) {
        ws.onmessage({ data: JSON.stringify(data) });
      }
    }
  }, msg);
}

/** Read all messages the client has sent to the "server". */
async function getClientMessages(
  page: import("@playwright/test").Page,
): Promise<Array<Record<string, unknown>>> {
  const raw: string[] = await page.evaluate(
    () => (window as any).__sentMessages || [],
  );
  return raw.map((s) => JSON.parse(s));
}

/** Wait for the mock WebSocket to be ready and complete the handshake. */
async function waitForConnection(page: import("@playwright/test").Page) {
  await page.waitForFunction(
    () => (window as any).__mockWsReady === true,
    null,
    { timeout: 5000 },
  );
  // Wait for all WebSocket onopen callbacks to fire and subscribe messages to be sent
  await page.waitForTimeout(200);
  // Broadcast handshake to all connections
  await broadcastSend(page, {
    type: "connection_established",
    conversation_ids: [],
  });
  await broadcastSend(page, {
    type: "subscribe_success",
    events: ["chat_stream", "chat_error", "tool_status", "chat_thinking"],
  });
  // Wait for chat WS to be identified via its subscribe message
  await page.waitForFunction(() => (window as any).__chatWs !== null, null, {
    timeout: 3000,
  });
  await expect(page.locator("text=Connected")).toBeVisible({ timeout: 3000 });
}

test.describe("Chat interrupt synchronization", () => {
  test("response should match the current request after interrupt, not the previous one", async ({
    page,
  }) => {
    await setupMockWebSocket(page);
    await page.goto("/");
    await waitForConnection(page);

    const input = page.locator("textarea").first();

    // --- User sends message A: "reply with 1" ---
    await input.fill("reply with 1");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(150);

    const allMsgs = await getClientMessages(page);
    const msgA = allMsgs.find(
      (m) => m.type === "chat_message" && m.content === "reply with 1",
    );
    expect(msgA).toBeTruthy();
    const requestIdA = msgA!.request_id as string;

    // Server starts streaming response for A
    await serverSend(page, {
      type: "chat_stream",
      message_id: "assistant-aaa",
      conversation_id: CONVERSATION_ID,
      request_id: requestIdA,
      content: "1",
      done: false,
    });
    await page.waitForTimeout(100);

    // Verify first response appeared
    await expect(
      page.locator(".message-assistant .message-content").first(),
    ).toContainText("1");

    // --- User sends message B (interrupt): "reply with 2" ---
    await input.fill("reply with 2");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(150);

    const allMsgs2 = await getClientMessages(page);
    const msgB = allMsgs2.find(
      (m) => m.type === "chat_message" && m.content === "reply with 2",
    );
    expect(msgB).toBeTruthy();
    const requestIdB = msgB!.request_id as string;
    expect(requestIdB).not.toBe(requestIdA);

    // Server sends interrupted signal for request A
    await serverSend(page, {
      type: "chat_stream",
      message_id: "assistant-aaa",
      conversation_id: CONVERSATION_ID,
      request_id: requestIdA,
      content: "",
      done: true,
      interrupted: true,
    });

    // Server sends correct response for request B
    await serverSend(page, {
      type: "chat_stream",
      message_id: "assistant-bbb",
      conversation_id: CONVERSATION_ID,
      request_id: requestIdB,
      content: "2",
      done: false,
    });
    await serverSend(page, {
      type: "chat_stream",
      message_id: "assistant-bbb",
      conversation_id: CONVERSATION_ID,
      request_id: requestIdB,
      content: "",
      done: true,
    });

    // Wait for render
    await expect(page.locator(".message-assistant")).toHaveCount(2, {
      timeout: 2000,
    });

    const assistantMessages = page.locator(
      ".message-assistant .message-content",
    );

    // First assistant response should contain "1"
    await expect(assistantMessages.nth(0)).toContainText("1");

    // Second assistant response should contain "2", NOT "1"
    const secondResponse = await assistantMessages.nth(1).innerText();
    expect(secondResponse.trim()).toBe("2");
  });

  test("stale chunks from old request_id should be dropped after interrupt", async ({
    page,
  }) => {
    await setupMockWebSocket(page);
    await page.goto("/");
    await waitForConnection(page);

    const input = page.locator("textarea").first();

    // User sends message A
    await input.fill("message A");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(150);

    const msgs1 = await getClientMessages(page);
    const msgA = msgs1.find(
      (m) => m.type === "chat_message" && m.content === "message A",
    );
    expect(msgA).toBeTruthy();
    const requestIdA = msgA!.request_id as string;

    // User sends message B (interrupt) BEFORE any response for A arrives
    await input.fill("message B");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(150);

    const msgs2 = await getClientMessages(page);
    const msgB = msgs2.find(
      (m) => m.type === "chat_message" && m.content === "message B",
    );
    expect(msgB).toBeTruthy();
    const requestIdB = msgB!.request_id as string;

    // Server sends a NON-done chunk for request A (stale — should be dropped)
    await serverSend(page, {
      type: "chat_stream",
      message_id: "assistant-stale",
      conversation_id: CONVERSATION_ID,
      request_id: requestIdA,
      content: "STALE CONTENT",
      done: false,
    });

    // Server sends done/interrupted for request A
    await serverSend(page, {
      type: "chat_stream",
      message_id: "assistant-stale",
      conversation_id: CONVERSATION_ID,
      request_id: requestIdA,
      content: "",
      done: true,
      interrupted: true,
    });

    // Server sends proper response for request B
    await serverSend(page, {
      type: "chat_stream",
      message_id: "assistant-fresh",
      conversation_id: CONVERSATION_ID,
      request_id: requestIdB,
      content: "CORRECT RESPONSE",
      done: false,
    });
    await serverSend(page, {
      type: "chat_stream",
      message_id: "assistant-fresh",
      conversation_id: CONVERSATION_ID,
      request_id: requestIdB,
      content: "",
      done: true,
    });

    // Wait for render
    await expect(
      page.locator(".message-assistant .message-content"),
    ).toHaveCount(1, { timeout: 2000 });

    // The stale content should NOT appear
    const allText = await page.locator(".chat-messages").innerText();
    expect(allText).not.toContain("STALE CONTENT");

    // Only response B should appear
    await expect(
      page.locator(".message-assistant .message-content").first(),
    ).toContainText("CORRECT RESPONSE");
  });

  test("triple interrupt should not compound the off-by-one", async ({
    page,
  }) => {
    await setupMockWebSocket(page);
    await page.goto("/");
    await waitForConnection(page);

    const input = page.locator("textarea").first();

    // Send 3 messages rapidly (each interrupts the previous)
    for (const msg of ["first", "second", "third"]) {
      await input.fill(msg);
      await page.keyboard.press("Enter");
      await page.waitForTimeout(80);
    }
    await page.waitForTimeout(150);

    // Collect all request IDs
    const allMsgs = await getClientMessages(page);
    const requestIds: string[] = [];
    for (const msg of ["first", "second", "third"]) {
      const found = allMsgs.find(
        (m) => m.type === "chat_message" && m.content === msg,
      );
      expect(found).toBeTruthy();
      requestIds.push(found!.request_id as string);
    }

    // Send interrupted for first two
    await serverSend(page, {
      type: "chat_stream",
      message_id: "a1",
      conversation_id: CONVERSATION_ID,
      request_id: requestIds[0],
      content: "",
      done: true,
      interrupted: true,
    });
    await serverSend(page, {
      type: "chat_stream",
      message_id: "a2",
      conversation_id: CONVERSATION_ID,
      request_id: requestIds[1],
      content: "",
      done: true,
      interrupted: true,
    });

    // Send actual response for the third (active) request
    await serverSend(page, {
      type: "chat_stream",
      message_id: "a3",
      conversation_id: CONVERSATION_ID,
      request_id: requestIds[2],
      content: "response to third",
      done: false,
    });
    await serverSend(page, {
      type: "chat_stream",
      message_id: "a3",
      conversation_id: CONVERSATION_ID,
      request_id: requestIds[2],
      content: "",
      done: true,
    });

    // Wait for render
    await expect(
      page.locator(".message-assistant .message-content"),
    ).toHaveCount(1, { timeout: 2000 });

    // Should have exactly 1 assistant message (for "third")
    await expect(
      page.locator(".message-assistant .message-content").first(),
    ).toContainText("response to third");
  });
});
