import { expect, test } from "@playwright/test";

const CURRENT_DB_SESSION_ID = "web-main-current";
const CURRENT_REF = "#202";
const OTHER_DB_SESSION_ID = "web-other";
const STALE_TERMINAL_SESSION_ID = "terminal-stale";

const webChatSessions = [
  {
    id: CURRENT_DB_SESSION_ID,
    ref: CURRENT_REF,
    external_id: CURRENT_DB_SESSION_ID,
    source: "codex",
    project_id: "proj-1",
    title: "Current Main Chat",
    status: "active",
    model: "gpt-5.4",
    message_count: 4,
    created_at: "2026-04-19T18:10:00Z",
    updated_at: "2026-04-19T18:15:00Z",
    seq_num: 202,
    summary_markdown: null,
    git_branch: "feature/mobile-refresh",
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
    id: OTHER_DB_SESSION_ID,
    ref: "#203",
    external_id: OTHER_DB_SESSION_ID,
    source: "claude",
    project_id: "proj-1",
    title: "Other Web Chat",
    status: "active",
    model: "sonnet",
    message_count: 10,
    created_at: "2026-04-19T18:20:00Z",
    updated_at: "2026-04-19T18:25:00Z",
    seq_num: 203,
    summary_markdown: null,
    git_branch: "main",
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: "normal",
    parent_session_id: null,
    session_type: "web_chat",
    terminal_context: null,
    sandbox_enabled: true,
    sandbox_policy_hash: "policy-web",
  },
];

async function seedLocalState(page: Parameters<typeof test>[0]["page"]) {
  await page.addInitScript(
    ({ sessionId }: { sessionId: string }) => {
      localStorage.setItem("gobby-conversation-id", sessionId);
      localStorage.setItem("gobby-db-session-id", sessionId);
      localStorage.setItem(
        "gobby-settings",
        JSON.stringify({
          model: "gpt-5.4",
          fontSize: 16,
          theme: "dark",
          defaultChatMode: "plan",
          postPlanChatMode: "bypass",
        }),
      );
    },
    { sessionId: CURRENT_DB_SESSION_ID },
  );
}

async function mockApi(
  page: Parameters<typeof test>[0]["page"],
  counters?: { currentMessageFetches: number; otherMessageFetches: number },
) {
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
                model: "gpt-5.4",
                theme: "dark",
                defaultChatMode: "plan",
                postPlanChatMode: "bypass",
                fontSize: 16,
              }),
      });
      return;
    }

    if (path === "/api/providers" || path === "/api/providers/models") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          providers:
            path === "/api/providers"
              ? [{ name: "codex", available: true }, { name: "claude", available: true }]
              : [
                  {
                    provider: "codex",
                    available: true,
                    default_model: "gpt-5.4",
                    models: [{ value: "gpt-5.4", label: "GPT-5.4", is_default: true }],
                  },
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

    if (path === "/api/projects") {
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
            created_at: "2026-04-19T18:00:00Z",
            updated_at: "2026-04-19T18:00:00Z",
            session_count: 2,
            open_task_count: 0,
            last_activity_at: "2026-04-19T18:15:00Z",
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
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ sessions: webChatSessions, total: webChatSessions.length }),
      });
      return;
    }

    if (path === `/api/sessions/${CURRENT_DB_SESSION_ID}`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session: webChatSessions[0],
        }),
      });
      return;
    }

    if (path.startsWith(`/api/chat/${CURRENT_DB_SESSION_ID}/messages`)) {
      if (counters) {
        counters.currentMessageFetches += 1;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          messages: [
            {
              id: "msg-1",
              role: "assistant",
              content: "Persisted main chat message",
              tool_calls: [],
              seq: 1,
              created_at: "2026-04-19T18:12:00Z",
            },
          ],
          max_seq: 1,
        }),
      });
      return;
    }

    if (path.startsWith(`/api/chat/${OTHER_DB_SESSION_ID}/messages`)) {
      if (counters) {
        counters.otherMessageFetches += 1;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          messages: [
            {
              id: "other-msg-1",
              role: "assistant",
              content: "Other session message",
              tool_calls: [],
              seq: 1,
              created_at: "2026-04-19T18:22:00Z",
            },
          ],
          max_seq: 1,
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
}

test.describe("Web Chat Restore And Plan Mode", () => {
  test("mobile refresh restores the same main-chat session", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await seedLocalState(page);
    const counters = { currentMessageFetches: 0, otherMessageFetches: 0 };
    await mockApi(page, counters);

    await page.routeWebSocket("**/ws", (ws) => {
      let sentPlan = false;
      ws.onMessage((raw) => {
        const message = JSON.parse(String(raw));
        if (message.type === "subscribe") {
          ws.send(
            JSON.stringify({
              type: "connection_established",
              conversation_ids: [CURRENT_DB_SESSION_ID],
            }),
          );
          ws.send(
            JSON.stringify({
              type: "subscribe_success",
              events: message.events ?? [],
            }),
          );
        } else if (message.type === "heartbeat" && !sentPlan) {
          sentPlan = true;
        }
      });
    });

    await page.goto("/");

    await expect(page.getByText("Persisted main chat message")).toBeVisible();
    await expect(page.getByText("Other session message")).toHaveCount(0);
    await expect
      .poll(async () => page.evaluate(() => localStorage.getItem("gobby-db-session-id")))
      .toBe(CURRENT_DB_SESSION_ID);

    await page.reload();

    await expect(page.getByText("Persisted main chat message")).toBeVisible();
    await expect(page.getByText("Other session message")).toHaveCount(0);
    expect(counters.currentMessageFetches).toBeGreaterThanOrEqual(2);
    expect(counters.otherMessageFetches).toBe(0);
  });

  test("stale terminal restore state fails clear instead of restoring the wrong main chat", async ({
    page,
  }) => {
    await page.addInitScript(
      ({ sessionId }: { sessionId: string }) => {
        localStorage.setItem("gobby-conversation-id", sessionId);
        localStorage.setItem("gobby-db-session-id", sessionId);
      },
      { sessionId: STALE_TERMINAL_SESSION_ID },
    );
    await mockApi(page);

    let staleMessageFetches = 0;
    await page.route("**/api/sessions", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ sessions: [webChatSessions[0]], total: 1 }),
      });
    });
    await page.route(`**/api/sessions/${STALE_TERMINAL_SESSION_ID}`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session: {
            id: STALE_TERMINAL_SESSION_ID,
            ref: "#999",
            external_id: "term-stale",
            source: "codex",
            project_id: "proj-1",
            title: "Stale Terminal",
            status: "active",
            model: "gpt-5.4",
            message_count: 1,
            created_at: "2026-04-19T18:05:00Z",
            updated_at: "2026-04-19T18:06:00Z",
            seq_num: 999,
            summary_markdown: null,
            git_branch: "main",
            usage_input_tokens: 0,
            usage_output_tokens: 0,
            had_edits: false,
            agent_depth: 0,
            chat_mode: null,
            parent_session_id: null,
            session_type: "terminal",
            terminal_context: { tmux_session: "stale" },
          },
        }),
      });
    });
    await page.route(`**/api/chat/${STALE_TERMINAL_SESSION_ID}/messages**`, async (route) => {
      staleMessageFetches += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          messages: [
            {
              id: "stale-msg-1",
              role: "assistant",
              content: "Stale terminal message",
              tool_calls: [],
              seq: 1,
              created_at: "2026-04-19T18:06:00Z",
            },
          ],
          max_seq: 1,
        }),
      });
    });

    await page.routeWebSocket("**/ws", (ws) => {
      ws.onMessage((raw) => {
        const message = JSON.parse(String(raw));
        if (message.type === "subscribe") {
          ws.send(
            JSON.stringify({
              type: "connection_established",
              conversation_ids: [CURRENT_DB_SESSION_ID],
            }),
          );
          ws.send(
            JSON.stringify({
              type: "subscribe_success",
              events: message.events ?? [],
            }),
          );
        }
      });
    });

    await page.goto("/");

    await expect(page.getByText("Start a conversation with Gobby")).toBeVisible();
    await expect(page.getByText("Stale terminal message")).toHaveCount(0);
    await expect
      .poll(async () => page.evaluate(() => localStorage.getItem("gobby-db-session-id")))
      .toBeNull();
    expect(staleMessageFetches).toBe(0);
  });

  test("approving a plan switches into the settings-selected post-plan mode", async ({
    page,
  }) => {
    await seedLocalState(page);
    await mockApi(page);

    await page.routeWebSocket("**/ws", (ws) => {
      let sentPlan = false;
      ws.onMessage((raw) => {
        const message = JSON.parse(String(raw));
        if (message.type === "subscribe") {
          ws.send(
            JSON.stringify({
              type: "connection_established",
              conversation_ids: [CURRENT_DB_SESSION_ID],
            }),
          );
          ws.send(
            JSON.stringify({
              type: "subscribe_success",
              events: message.events ?? [],
            }),
          );
        } else if (message.type === "heartbeat" && !sentPlan) {
          sentPlan = true;
          setTimeout(() => {
            ws.send(
              JSON.stringify({
                type: "plan_pending_approval",
                conversation_id: CURRENT_DB_SESSION_ID,
                plan_content: "## Plan\n\n1. Fix restore state\n2. Add tests",
              }),
            );
          }, 250);
        } else if (message.type === "plan_approval_response") {
          ws.send(
            JSON.stringify({
              type: "mode_changed",
              conversation_id: CURRENT_DB_SESSION_ID,
              mode: "bypass",
              reason: "plan_approved",
            }),
          );
        }
      });
    });

    await page.goto("/");

    await expect(
      page.getByRole("button", { name: "Approve & Execute" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Approve & Execute" }).click();

    await expect(page.getByRole("radio", { name: "Auto", exact: true })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });
});
