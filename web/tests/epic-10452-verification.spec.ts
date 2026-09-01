import { test, expect } from "@playwright/test";

/**
 * Playwright verification tests for epic #10452:
 * - #10455: Auto-send feedback on plan_changes_requested
 * - #10456: Retry fetchProjects with backoff
 * - #10458: Wire sendProjectChange in App.tsx via useEffect
 */

const CONVERSATION_ID = "test-epic-10452";
const STORAGE_KEY = `gobby-chat-${CONVERSATION_ID}`;
const CONVERSATION_ID_KEY = "gobby-conversation-id";

const currentSession = {
  id: CONVERSATION_ID,
  ref: "#10452",
  external_id: CONVERSATION_ID,
  source: "codex",
  project_id: "proj-gobby",
  title: "Epic 10452 Verification",
  status: "active",
  model: "gpt-5.4",
  message_count: 0,
  created_at: "2026-08-31T08:00:00Z",
  updated_at: "2026-08-31T08:00:00Z",
  seq_num: 10452,
  summary_markdown: null,
  git_branch: "main",
  usage_input_tokens: 0,
  usage_output_tokens: 0,
  had_edits: false,
  agent_depth: 0,
  chat_mode: "plan",
  agent_run_id: null,
  parent_session_id: null,
  session_type: "web_chat",
  terminal_context: null,
};

const mockProjects = [
  {
    id: "proj-personal",
    name: "Personal",
    checkout: { machine_id: "machine-1", root_path: "/tmp/personal" },
  },
  {
    id: "proj-gobby",
    name: "gobby",
    checkout: { machine_id: "machine-1", root_path: "/tmp/gobby" },
  },
  {
    id: "proj-other",
    name: "other-project",
    checkout: { machine_id: "machine-1", root_path: "/tmp/other" },
  },
];

function setupMockWebSocket(page: import("@playwright/test").Page) {
  return page.addInitScript(
    ({ convId, storageKey, convIdKey }) => {
      localStorage.setItem(convIdKey, convId);
      localStorage.setItem("gobby-db-session-id", convId);
      localStorage.setItem(storageKey, "[]");

      (window as any).__sentMessages = [] as string[];
      (window as any).__mockWsReady = false;
      (window as any).__allMockWs = [] as any[];
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

async function broadcastSend(
  page: import("@playwright/test").Page,
  msg: Record<string, unknown>,
) {
  return page.evaluate((data) => {
    let delivered = 0;
    for (const ws of (window as any).__allMockWs || []) {
      if (ws.onmessage) {
        ws.onmessage({ data: JSON.stringify(data) });
        delivered += 1;
      }
    }
    return delivered;
  }, msg);
}

async function getClientMessages(
  page: import("@playwright/test").Page,
): Promise<Array<Record<string, unknown>>> {
  const raw: string[] = await page.evaluate(
    () => (window as any).__sentMessages || [],
  );
  return raw.map((s) => JSON.parse(s));
}

async function clearClientMessages(page: import("@playwright/test").Page) {
  await page.evaluate(() => {
    (window as any).__sentMessages = [];
  });
}

async function waitForConnection(page: import("@playwright/test").Page) {
  await page.waitForFunction(
    () => (window as any).__mockWsReady === true,
    null,
    { timeout: 5000 },
  );
  await page.waitForTimeout(200);
  await broadcastSend(page, {
    type: "connection_established",
    conversation_ids: [],
  });
  await broadcastSend(page, {
    type: "subscribe_success",
    events: ["chat_stream", "chat_error", "tool_status", "chat_thinking"],
  });
  await page.waitForFunction(() => (window as any).__chatWs !== null, null, {
    timeout: 3000,
  });
}

/** Read the actual conversation_id the app is using from its sent messages. */
async function getAppConversationId(
  page: import("@playwright/test").Page,
): Promise<string | null> {
  const msgs = await getClientMessages(page);
  // The subscribe message or any message with conversation_id reveals the app's actual ID
  for (const m of [...msgs].reverse()) {
    if (m.conversation_id && typeof m.conversation_id === "string") {
      return m.conversation_id;
    }
  }
  return null;
}

function setupApiMocks(page: import("@playwright/test").Page) {
  page.route("**/api/auth/status", (route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ authenticated: true }),
    });
  });

  page.route("**/api/projects", (route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(mockProjects),
    });
  });

  page.route("**/api/config/values", (route) => {
    const uiSettings = {
      selectedProjectId: "proj-gobby",
      selectedProvider: "claude",
    };
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        revision: 1,
        desired: { ui_settings: uiSettings },
        active: { ui_settings: uiSettings },
        secret_set: {},
        pending_restart_keys: [],
        failed_live_keys: {},
      }),
    });
  });

  page.route("**/api/files/projects", (route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(mockProjects),
    });
  });

  page.route("**/api/sessions*", (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = path.endsWith("/messages")
      ? { messages: [] }
      : path === `/api/sessions/${CONVERSATION_ID}`
        ? { session: currentSession }
        : { sessions: [currentSession], total: 1, next_cursor: null };
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
  page.route("**/api/files/tree*", (route) => {
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });
  page.route("**/api/files/git-status*", (route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ branch: "main", files: {} }),
    });
  });
}

// ============================================================
// #10455: Auto-send feedback on plan_changes_requested
// ============================================================
test.describe("#10455: Plan feedback auto-send", () => {
  test("clicking Request Changes → entering feedback → mode_changed triggers auto-send", async ({
    page,
  }) => {
    setupApiMocks(page);
    await setupMockWebSocket(page);
    await page.goto("/");
    await waitForConnection(page);

    // Read the app's actual conversation_id from its sent messages
    const appConvId = await getAppConversationId(page);
    expect(appConvId).toBeTruthy();

    // 1. Server sends plan_pending_approval to show PlanApprovalBar
    const pendingRecipients = await broadcastSend(page, {
      type: "plan_pending_approval",
      conversation_id: appConvId,
      plan_content: "## Plan\n\n1. Refactor the auth module\n2. Add tests",
    });
    expect(pendingRecipients).toBeGreaterThan(0);

    // 2. Wait for PlanApprovalBar to appear
    const requestChangesBtn = page.getByTestId("plan-strip-reject");
    await expect(requestChangesBtn).toBeVisible({ timeout: 3000 });

    // 3. Click Request Changes to show feedback form
    await requestChangesBtn.click();

    // 4. Enter feedback
    const feedbackInput = page.getByTestId("plan-strip-feedback");
    await expect(feedbackInput).toBeVisible({ timeout: 2000 });
    await feedbackInput.fill("Please add error handling to the auth module");

    // 5. Click Send Feedback
    const sendFeedbackBtn = page.getByTestId("plan-strip-send");
    await sendFeedbackBtn.click();

    // 6. Verify plan_approval_response was sent with request_changes decision
    const msgsAfterFeedback = await getClientMessages(page);
    const approvalResponse = msgsAfterFeedback.find(
      (m) =>
        m.type === "plan_approval_response" && m.decision === "request_changes",
    );
    expect(approvalResponse).toBeTruthy();
    expect(approvalResponse!.feedback).toBe(
      "Please add error handling to the auth module",
    );

    // Read the actual conversation_id from the response (in case it differs from localStorage)
    const actualConvId =
      (approvalResponse!.conversation_id as string) || appConvId;

    // 7. Clear messages to isolate the auto-send
    await clearClientMessages(page);

    // 8. Server sends mode_changed with reason plan_changes_requested
    //    Use the app's actual conversation_id so the handler's ID check passes
    await broadcastSend(page, {
      type: "mode_changed",
      mode: "plan",
      reason: "plan_changes_requested",
      ...(actualConvId ? { conversation_id: actualConvId } : {}),
    });

    // 9. Wait for the 200ms delay + auto-send
    await page.waitForTimeout(500);

    // 10. Verify the feedback was auto-sent as a chat_message
    const autoSentMsgs = await getClientMessages(page);
    const chatMsg = autoSentMsgs.find(
      (m) =>
        m.type === "chat_message" &&
        m.content === "Please add error handling to the auth module",
    );
    expect(chatMsg).toBeTruthy();
  });
});

// ============================================================
// #10456: Retry fetchProjects with backoff
// ============================================================
test.describe("#10456: fetchProjects retry with backoff", () => {
  test("retries on failure and shows project selector once successful", async ({
    page,
  }) => {
    let fetchAttempt = 0;

    // Mock projects endpoint to fail first 2 attempts, succeed on 3rd
    await page.route("**/api/auth/status", (route) => {
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ authenticated: true }),
      });
    });

    await page.route("**/api/projects", (route) => {
      fetchAttempt++;
      if (fetchAttempt <= 2) {
        route.fulfill({ status: 500, body: "Internal Server Error" });
      } else {
        route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(mockProjects),
        });
      }
    });

    await page.route("**/api/sessions*", (route) => {
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: "[]",
      });
    });
    await page.route("**/api/files/tree*", (route) => {
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: "[]",
      });
    });
    await page.route("**/api/files/git-status*", (route) => {
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ branch: "main", files: {} }),
      });
    });

    await setupMockWebSocket(page);
    await page.goto("/");
    await waitForConnection(page);

    // Wait for the project selector to appear — means retries succeeded.
    // Backoff: 2s + 4s = 6s total wait before 3rd attempt
    const projectSelector = page.getByRole("group", {
      name: "Project selector",
    });
    await expect(projectSelector).toBeVisible({ timeout: 15000 });
    await expect(
      projectSelector.getByRole("radio", { name: "gobby" }),
    ).toBeChecked();

    // Verify all 3 attempts were made
    expect(fetchAttempt).toBe(3);
  });
});

// ============================================================
// #10458: Wire sendProjectChange in App.tsx via useEffect
// ============================================================
test.describe("#10458: sendProjectChange on project switch", () => {
  test("switching projects starts a fresh chat without rebinding the previous conversation", async ({
    page,
  }) => {
    setupApiMocks(page);
    await setupMockWebSocket(page);
    await page.goto("/");
    await waitForConnection(page);

    // Wait for project selector to appear (projects loaded, default selected)
    const projectSelector = page.getByRole("group", {
      name: "Project selector",
    });
    await expect(projectSelector).toBeVisible({ timeout: 5000 });

    // Default project is "gobby".
    await expect(
      projectSelector.getByRole("radio", { name: "gobby" }),
    ).toBeChecked({ timeout: 2000 });

    // Clear messages to isolate the project change
    await clearClientMessages(page);

    // Switch to Personal project by clicking "Personal" button in the selector
    const personalBtn = projectSelector.getByRole("radio", {
      name: "Personal",
    });
    await personalBtn.click();

    // Wait for the useEffect to fire
    await page.waitForTimeout(300);

    await expect(personalBtn).toBeChecked();
    await expect(
      page.getByRole("button", { name: "New Session" }),
    ).toBeVisible();

    // Project changes start a fresh chat. The old conversation must not be
    // rebound to the new project before that fresh session exists.
    const msgs = await getClientMessages(page);
    const setProjectMsg = msgs.find(
      (m) => m.type === "set_project" && m.project_id === "proj-personal",
    );
    expect(setProjectMsg).toBeUndefined();
  });

  test("initial project load sends set_project for default project", async ({
    page,
  }) => {
    setupApiMocks(page);
    await setupMockWebSocket(page);
    await page.goto("/");
    await waitForConnection(page);

    // Wait for project selector to appear — means projects loaded and default set
    const projectSelector = page.getByRole("group", {
      name: "Project selector",
    });
    await expect(projectSelector).toBeVisible({ timeout: 5000 });

    // Allow time for the useEffect to fire after effectiveProjectId is set
    await page.waitForTimeout(500);

    // Check that set_project was sent for the default project (gobby)
    const msgs = await getClientMessages(page);
    const setProjectMsgs = msgs.filter((m) => m.type === "set_project");
    expect(setProjectMsgs.length).toBeGreaterThanOrEqual(1);
    // The default project should be the first non-Personal project (gobby)
    const gobbyMsg = setProjectMsgs.find((m) => m.project_id === "proj-gobby");
    expect(gobbyMsg).toBeTruthy();
  });
});
