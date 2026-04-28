import { expect, test } from "@playwright/test";

const CURRENT_CONVERSATION_ID = "web-current-ext";
const CURRENT_DB_SESSION_ID = "web-current";

const sessions = [
  {
    id: "terminal-1",
    ref: "#201",
    external_id: "terminal-ext-1",
    source: "claude",
    project_id: "proj-1",
    title: "Terminal Session",
    status: "active",
    model: "sonnet",
    message_count: 3,
    created_at: "2026-04-08T12:00:00Z",
    updated_at: "2026-04-08T12:05:00Z",
    seq_num: 201,
    summary_markdown: null,
    git_branch: "main",
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: "plan",
    parent_session_id: null,
    session_type: "terminal",
    terminal_context: { session_name: "dev" },
    sandbox_enabled: false,
    sandbox_policy_hash: null,
  },
  {
    id: CURRENT_DB_SESSION_ID,
    ref: "#202",
    external_id: CURRENT_CONVERSATION_ID,
    source: "claude",
    project_id: "proj-1",
    title: "Current Web Chat",
    status: "active",
    model: "sonnet",
    message_count: 8,
    created_at: "2026-04-08T12:10:00Z",
    updated_at: "2026-04-08T12:15:00Z",
    seq_num: 202,
    summary_markdown: null,
    git_branch: "main",
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: "plan",
    parent_session_id: null,
    session_type: "web_chat",
    terminal_context: null,
    sandbox_enabled: true,
    sandbox_policy_hash: "policy-web",
  },
  {
    id: "web-other",
    ref: "#203",
    external_id: "web-other-ext",
    source: "codex",
    project_id: "proj-1",
    title: "Other Web Chat",
    status: "active",
    model: "gpt-5.4",
    message_count: 11,
    created_at: "2026-04-08T12:20:00Z",
    updated_at: "2026-04-08T12:25:00Z",
    seq_num: 203,
    summary_markdown: null,
    git_branch: "main",
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: "plan",
    parent_session_id: null,
    session_type: "web_chat",
    terminal_context: null,
    sandbox_enabled: true,
    sandbox_policy_hash: "policy-web",
  },
];

test("activity panel shows non-current web chats with a web badge", async ({
  page,
}) => {
  await page.addInitScript(
    ({
      conversationId,
      dbSessionId,
    }: {
      conversationId: string;
      dbSessionId: string;
    }) => {
      localStorage.setItem("gobby-conversation-id", conversationId);
      localStorage.setItem("gobby-db-session-id", dbSessionId);
      localStorage.setItem("gobby-activity-panel-pinned", "true");
      localStorage.setItem("gobby-activity-panel-tab", "sessions");
      localStorage.setItem(
        "gobby-settings",
        JSON.stringify({
          model: "opus",
          fontSize: 16,
          theme: "dark",
          defaultChatMode: "plan",
        }),
      );
    },
    {
      conversationId: CURRENT_CONVERSATION_ID,
      dbSessionId: CURRENT_DB_SESSION_ID,
    },
  );

  await page.routeWebSocket("**/ws", (ws) => {
    ws.onMessage((raw) => {
      let message: Record<string, unknown> | null = null;
      try {
        message = JSON.parse(String(raw));
      } catch {
        message = null;
      }

      const type = message?.type;
      if (type === "subscribe") {
        ws.send(
          JSON.stringify({
            type: "connection_established",
            conversation_ids: [CURRENT_CONVERSATION_ID],
          }),
        );
        ws.send(
          JSON.stringify({
            type: "subscribe_success",
            events: message?.events ?? [],
          }),
        );
      }
    });
  });

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
                defaultChatMode: "plan",
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
          providers: [{ name: "claude", available: true }],
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

    if (path === "/api/projects" || path === "/api/files/projects") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: "proj-1",
            name: "project-one",
            display_name: "Project One",
            repo_path: "/tmp/project-one",
            github_url: null,
            github_repo: null,
            linear_team_id: null,
            approval_rules: [],
            created_at: "2026-04-08T12:00:00Z",
            updated_at: "2026-04-08T12:00:00Z",
            session_count: 0,
            open_task_count: 0,
            last_activity_at: null,
          },
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
      const body =
        status === "paused" || status === "handoff_ready"
          ? { sessions: [] }
          : { sessions, total: sessions.length };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
      return;
    }

    if (path === `/api/chat/${CURRENT_CONVERSATION_ID}/messages`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ messages: [] }),
      });
      return;
    }

    if (path === `/api/sessions/${CURRENT_DB_SESSION_ID}`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session: sessions.find((session) => session.id === CURRENT_DB_SESSION_ID),
        }),
      });
      return;
    }

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({}),
    });
  });

  await page.goto("/");

  await expect(page.locator(".activity-panel-mobile-trigger")).toContainText("Sessions");
  await expect(page.locator(".session-entry")).toHaveCount(2);
  await expect(page.locator(".activity-panel-content")).toContainText(
    "#201: Terminal Session",
  );
  await expect(page.locator(".activity-panel-content")).toContainText(
    "#203: Other Web Chat",
  );

  await expect(page.locator(".activity-panel-content")).not.toContainText(
    "Current Web Chat",
  );
  await expect(page.locator(".activity-panel-content")).toContainText("tmux");
  await expect(page.locator(".activity-panel-content")).toContainText("web");
  await expect(page.locator(".activity-panel-content")).toContainText("SB");
});

test("activity panel refreshes sessions after a session_event websocket message", async ({
  page,
}) => {
  await page.addInitScript(
    ({
      conversationId,
      dbSessionId,
    }: {
      conversationId: string;
      dbSessionId: string;
    }) => {
      localStorage.setItem("gobby-conversation-id", conversationId);
      localStorage.setItem("gobby-db-session-id", dbSessionId);
      localStorage.setItem("gobby-activity-panel-pinned", "true");
      localStorage.setItem("gobby-activity-panel-tab", "sessions");
      localStorage.setItem(
        "gobby-settings",
        JSON.stringify({
          model: "opus",
          fontSize: 16,
          theme: "dark",
          defaultChatMode: "plan",
        }),
      );
    },
    {
      conversationId: CURRENT_CONVERSATION_ID,
      dbSessionId: CURRENT_DB_SESSION_ID,
    },
  );

  const sessionEventSenders: Array<() => void> = [];
  await page.routeWebSocket("**/ws", (ws) => {
    sessionEventSenders.push(() => {
      ws.send(
        JSON.stringify({
          type: "session_event",
          event: "session_created",
          session_id: "web-other",
          timestamp: "2026-04-08T12:26:00Z",
        }),
      );
    });

    ws.onMessage((raw) => {
      let message: Record<string, unknown> | null = null;
      try {
        message = JSON.parse(String(raw));
      } catch {
        message = null;
      }

      const type = message?.type;
      if (type === "subscribe") {
        ws.send(
          JSON.stringify({
            type: "connection_established",
            conversation_ids: [CURRENT_CONVERSATION_ID],
          }),
        );
        ws.send(
          JSON.stringify({
            type: "subscribe_success",
            events: message?.events ?? [],
          }),
        );
      }
    });
  });

  let catalogSessions = [sessions[1], sessions[0]];
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
                defaultChatMode: "plan",
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
          providers: [{ name: "claude", available: true }],
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

    if (path === "/api/projects" || path === "/api/files/projects") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: "proj-1",
            name: "project-one",
            display_name: "Project One",
            repo_path: "/tmp/project-one",
            github_url: null,
            github_repo: null,
            linear_team_id: null,
            approval_rules: [],
            created_at: "2026-04-08T12:00:00Z",
            updated_at: "2026-04-08T12:00:00Z",
            session_count: 0,
            open_task_count: 0,
            last_activity_at: null,
          },
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
      const body =
        status === "paused" || status === "handoff_ready"
          ? { sessions: [] }
          : { sessions: catalogSessions, total: catalogSessions.length };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
      return;
    }

    if (path === `/api/chat/${CURRENT_CONVERSATION_ID}/messages`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ messages: [] }),
      });
      return;
    }

    if (path === `/api/sessions/${CURRENT_DB_SESSION_ID}`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session: sessions.find((session) => session.id === CURRENT_DB_SESSION_ID),
        }),
      });
      return;
    }

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({}),
    });
  });

  await page.goto("/");

  await expect(page.locator(".activity-panel-mobile-trigger")).toContainText("Sessions");
  await expect(page.locator(".session-entry")).toHaveCount(1);
  await expect(page.locator(".session-entry")).toContainText(["#201: Terminal Session"]);
  await expect(page.locator(".activity-panel-content")).not.toContainText("Other Web Chat");

  catalogSessions = [sessions[1], sessions[0], sessions[2]];
  if (sessionEventSenders.length === 0) {
    throw new Error("WebSocket route was not opened");
  }
  for (const sendSessionEvent of sessionEventSenders) {
    sendSessionEvent();
  }

  await expect(page.locator(".session-entry")).toHaveCount(2);
  await expect(page.locator(".activity-panel-content")).toContainText(
    "#201: Terminal Session",
  );
  await expect(page.locator(".activity-panel-content")).toContainText(
    "#203: Other Web Chat",
  );
});
