import { expect, test } from "@playwright/test";

const CURRENT_CONVERSATION_ID = "web-current-ext";
const CURRENT_DB_SESSION_ID = "web-current";
const OTHER_CONVERSATION_ID = "web-other-ext";
const OTHER_DB_SESSION_ID = "web-other";

const sessions = [
  {
    id: CURRENT_DB_SESSION_ID,
    ref: "#202",
    external_id: CURRENT_CONVERSATION_ID,
    source: "claude",
    project_id: "proj-1",
    title: "Current Web Chat",
    status: "active",
    model: "sonnet",
    message_count: 3,
    created_at: "2026-04-08T12:10:00Z",
    updated_at: "2026-04-08T12:15:00Z",
    seq_num: 202,
    summary_markdown: null,
    git_branch: "main",
    usage_input_tokens: 100,
    usage_output_tokens: 20,
    had_edits: false,
    agent_depth: 0,
    chat_mode: "accept_edits",
    agent_run_id: null,
    parent_session_id: null,
    session_type: "web_chat",
    terminal_context: null,
  },
  {
    id: OTHER_DB_SESSION_ID,
    ref: "#203",
    external_id: OTHER_CONVERSATION_ID,
    source: "codex",
    project_id: "proj-1",
    title: "Other Web Chat",
    status: "active",
    model: "gpt-5.4",
    message_count: 6,
    created_at: "2026-04-08T12:20:00Z",
    updated_at: "2026-04-08T12:25:00Z",
    seq_num: 203,
    summary_markdown: null,
    git_branch: "feature/swap",
    usage_input_tokens: 200,
    usage_output_tokens: 40,
    had_edits: false,
    agent_depth: 0,
    chat_mode: "accept_edits",
    agent_run_id: null,
    parent_session_id: null,
    session_type: "web_chat",
    terminal_context: null,
  },
];

function setupMockWebSocket(page: import("@playwright/test").Page) {
  return page.addInitScript(
    ({ currentConversationId, currentDbSessionId }) => {
      localStorage.setItem("gobby-conversation-id", currentConversationId);
      localStorage.setItem("gobby-db-session-id", currentDbSessionId);
      localStorage.setItem(
        "gobby-settings",
        JSON.stringify({
          model: "opus",
          fontSize: 16,
          theme: "dark",
          defaultChatMode: "accept_edits",
        }),
      );

      (window as any).__sentMessages = [] as string[];
      (window as any).__mockWsReady = false;
      (window as any).__allMockWs = [] as any[];
      (window as any).__chatWs = null as any;

      const OriginalWebSocket = window.WebSocket;

      (window as any).WebSocket = function (url: string, ...rest: any[]) {
        if (typeof url === "string" && url.includes("/ws") && !url.includes("vite")) {
          let _onmessage: ((ev: { data: string }) => void) | null = null;
          let _onopen: (() => void) | null = null;
          let _onclose: (() => void) | null = null;

          const mockWs = {
            readyState: 1,
            url,
            send(data: string) {
              (window as any).__sentMessages.push(data);
              try {
                const parsed = JSON.parse(data);
                if (parsed.type === "subscribe" && parsed.events?.includes("chat_stream")) {
                  (window as any).__chatWs = mockWs;
                }
              } catch {
                // Ignore malformed test traffic
              }
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
      Object.defineProperty((window as any).WebSocket, "CONNECTING", { value: 0 });
      Object.defineProperty((window as any).WebSocket, "CLOSING", { value: 2 });
    },
    {
      currentConversationId: CURRENT_CONVERSATION_ID,
      currentDbSessionId: CURRENT_DB_SESSION_ID,
    },
  );
}

async function serverSend(
  page: import("@playwright/test").Page,
  msg: Record<string, unknown>,
) {
  await page.evaluate((data) => {
    const chatWs = (window as any).__chatWs;
    if (chatWs?.onmessage) {
      chatWs.onmessage({ data: JSON.stringify(data) });
      return;
    }
    for (const ws of (window as any).__allMockWs || []) {
      if (ws.onmessage) {
        ws.onmessage({ data: JSON.stringify(data) });
      }
    }
  }, msg);
}

async function getClientMessages(
  page: import("@playwright/test").Page,
): Promise<Array<Record<string, unknown>>> {
  const raw: string[] = await page.evaluate(() => (window as any).__sentMessages || []);
  return raw.map((msg) => JSON.parse(msg));
}

async function waitForConnection(page: import("@playwright/test").Page) {
  await page.waitForFunction(() => (window as any).__mockWsReady === true, null, {
    timeout: 5000,
  });
  await page.waitForTimeout(200);
  await serverSend(page, {
    type: "connection_established",
    conversation_ids: [CURRENT_CONVERSATION_ID],
  });
  await serverSend(page, {
    type: "subscribe_success",
    events: [
      "chat_stream",
      "chat_error",
      "tool_status",
      "chat_thinking",
      "session_message",
    ],
  });
}

test("can swap to another web chat, send a message, and receive a response", async ({
  page,
}) => {
  await setupMockWebSocket(page);

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();

    if (path === "/api/auth/status") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ auth_required: false, authenticated: true }),
      });
      return;
    }

    if (path === "/api/config/ui-settings") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body:
          method === "PUT"
            ? JSON.stringify({ ok: true })
            : JSON.stringify({
                selectedProjectId: "proj-1",
                model: "opus",
                theme: "dark",
                defaultChatMode: "accept_edits",
                fontSize: 16,
              }),
      });
      return;
    }

    if (path === "/api/providers") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          providers: [
            { name: "claude", available: true },
            { name: "codex", available: true },
          ],
        }),
      });
      return;
    }

    if (path === "/api/voice/status") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ enabled: false, stt_available: false }),
      });
      return;
    }

    if (path === "/api/files/projects") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          { id: "proj-1", name: "Project One", repo_path: "/tmp/project-one" },
        ]),
      });
      return;
    }

    if (path === "/api/agents/running") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ agents: [] }),
      });
      return;
    }

    if (path === "/api/sessions") {
      const status = url.searchParams.get("status");
      if (status === "paused") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ sessions: [] }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ sessions, total: sessions.length }),
      });
      return;
    }

    if (path === `/api/chat/${CURRENT_CONVERSATION_ID}/messages`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          messages: [
            {
              id: "current-msg-1",
              role: "assistant",
              content: "Current chat response",
              created_at: "2026-04-08T12:15:00Z",
              seq: 1,
            },
          ],
        }),
      });
      return;
    }

    if (path === `/api/sessions/${CURRENT_DB_SESSION_ID}/messages`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          messages: [
            {
              id: "current-rendered-1",
              role: "assistant",
              content: "Current chat response",
              timestamp: "2026-04-08T12:15:00Z",
              content_blocks: [{ type: "text", content: "Current chat response" }],
            },
          ],
        }),
      });
      return;
    }

    if (path === `/api/sessions/${CURRENT_DB_SESSION_ID}`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ session: sessions[0] }),
      });
      return;
    }

    if (path === `/api/sessions/${OTHER_DB_SESSION_ID}/messages`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          messages: [
            {
              id: "other-rendered-1",
              role: "assistant",
              content: "Other chat history",
              timestamp: "2026-04-08T12:25:00Z",
              content_blocks: [{ type: "text", content: "Other chat history" }],
            },
          ],
        }),
      });
      return;
    }

    if (path === `/api/sessions/${OTHER_DB_SESSION_ID}`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ session: sessions[1] }),
      });
      return;
    }

    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ error: `Unhandled route: ${path}` }),
    });
  });

  await page.goto("/");
  await waitForConnection(page);

  await expect(page.getByRole("button", { name: /#202 current web chat/i })).toBeVisible();

  await page.getByRole("button", { name: /#202 current web chat/i }).click();
  await expect(
    page.getByRole("dialog", { name: "Command palette" }),
  ).toBeVisible();
  await page.getByText("Other Web Chat").click();

  await expect(page.getByRole("button", { name: /#203 other web chat/i })).toBeVisible();
  await expect(page.getByText("Attach")).toHaveCount(0);
  await expect(page.getByText("Other chat history")).toBeVisible();

  const input = page.locator("textarea").first();
  await input.fill("Hello after swap");
  await page.keyboard.press("Enter");

  const outgoing = await getClientMessages(page);
  const chatMessage = [...outgoing]
    .reverse()
    .find((msg) => msg.type === "chat_message" && msg.content === "Hello after swap");
  expect(chatMessage).toBeTruthy();
  expect(chatMessage?.conversation_id).toBe(OTHER_CONVERSATION_ID);

  await serverSend(page, {
    type: "chat_stream",
    message_id: "assistant-swapped",
    conversation_id: OTHER_CONVERSATION_ID,
    request_id: chatMessage?.request_id,
    content: "Response from swapped chat",
    done: false,
  });
  await serverSend(page, {
    type: "chat_stream",
    message_id: "assistant-swapped",
    conversation_id: OTHER_CONVERSATION_ID,
    request_id: chatMessage?.request_id,
    content: "",
    done: true,
  });

  await expect(page.getByText("Response from swapped chat")).toBeVisible();

  await page.screenshot({
    path: "test-results/web-chat-swap-send-respond.png",
    fullPage: true,
  });
});
