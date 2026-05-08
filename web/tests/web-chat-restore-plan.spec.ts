import { expect, test } from "@playwright/test";

const CURRENT_DB_SESSION_ID = "web-main-current";
const CURRENT_REF = "#202";
const TASK_LINKED_DB_SESSION_ID = "web-task-linked";
const OTHER_DB_SESSION_ID = "web-other";
const STALE_TERMINAL_SESSION_ID = "terminal-stale";
const CLAIMED_TASK_ID = "task-14425";
const CLAIMED_TASK_REF = 14425;

const inProgressStage = {
  name: "development",
  display_name: "Development",
  category: "development",
  state: "in_progress",
  review_policy: "none",
  updated_at: "2026-04-19T18:30:00Z",
  position: 1,
};

const claimedTask = {
  id: CLAIMED_TASK_ID,
  ref: `#${CLAIMED_TASK_REF}`,
  title: "Verify web chat task state survives refresh",
  status: "in_progress",
  priority: 2,
  task_type: "bug",
  parent_task_id: null,
  created_at: "2026-04-19T18:30:00Z",
  updated_at: "2026-04-19T18:35:00Z",
  seq_num: CLAIMED_TASK_REF,
  path_cache: String(CLAIMED_TASK_REF),
  assignee: null,
  agent_name: null,
  sequence_order: null,
  start_date: null,
  due_date: null,
  project_id: "proj-1",
  claimed_by_session_id: CURRENT_DB_SESSION_ID,
  category: "test",
  labels: ["web-chat", "tasks", "persistence"],
  description: "Refresh should rehydrate task ownership and task-linked session state.",
  validation_status: "pending",
  validation_feedback: null,
  validation_criteria: "Refresh keeps claimed task state visible.",
  validation_fail_count: 0,
  validation_override_reason: null,
  closed_at: null,
  closed_reason: null,
  closed_commit_sha: null,
  commits: [],
  closed_in_session_id: null,
  escalated_at: null,
  escalation_reason: null,
  pre_escalation_status: null,
  created_in_session_id: CURRENT_DB_SESSION_ID,
  complexity_score: null,
  is_expanded: false,
  expansion_status: "not_expanded",
  github_pr_number: null,
  github_repo: null,
  allow_automation: false,
  yolo: false,
  isolation: "worktree",
  dispatch_failure_count: 0,
  additional_skills: [],
  assigned_agent: null,
  current_stage: inProgressStage,
  stages: [
    {
      name: "planning",
      display_name: "Planning",
      category: "development",
      state: "done",
      review_policy: "none",
      updated_at: "2026-04-19T18:29:00Z",
      position: 0,
    },
    inProgressStage,
  ],
  state: {
    owner_session_id: CURRENT_DB_SESSION_ID,
    current_stage: inProgressStage,
    is_claimed: true,
    is_closed: false,
    is_escalated: false,
    is_blocked: false,
    is_merge_ready: false,
    closed_at: null,
    closed_reason: null,
    closed_in_session_id: null,
    closed_commit_sha: null,
    escalated_at: null,
    escalation_reason: null,
  },
};

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
    transcript_path: null,
    digest_markdown: null,
    agent_run_id: null,
    claimed_task_refs: [CLAIMED_TASK_REF],
    created_task_refs: [14420],
    closed_task_refs: [],
  },
  {
    id: TASK_LINKED_DB_SESSION_ID,
    ref: "#204",
    external_id: TASK_LINKED_DB_SESSION_ID,
    source: "codex",
    project_id: "proj-1",
    title: "Task Linked Web Chat",
    status: "active",
    model: "gpt-5.4",
    message_count: 2,
    created_at: "2026-04-19T18:27:00Z",
    updated_at: "2026-04-19T18:32:00Z",
    seq_num: 204,
    summary_markdown: null,
    digest_markdown: null,
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
    transcript_path: "/tmp/task-linked-transcript.jsonl",
    agent_run_id: null,
    claimed_task_refs: [CLAIMED_TASK_REF],
    created_task_refs: [],
    closed_task_refs: [14410],
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
    transcript_path: null,
    digest_markdown: null,
    agent_run_id: null,
    claimed_task_refs: [],
    created_task_refs: [14001],
    closed_task_refs: [14002],
  },
];

type MockApiCounters = {
  currentMessageFetches?: number;
  otherMessageFetches?: number;
  otherSessionDetailFetches?: number;
  taskLinkedSessionDetailFetches?: number;
};

function bumpCounter(counters: MockApiCounters | undefined, key: keyof MockApiCounters) {
  if (!counters) return;
  counters[key] = (counters[key] ?? 0) + 1;
}

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
  counters?: MockApiCounters,
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

    if (path === "/api/stages/registry") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          stages: [
            {
              name: "planning",
              display_name: "Planning",
              category: "development",
              state: "ready",
              review_policy: "none",
              position: 0,
              sequence_order: 0,
            },
            {
              name: "development",
              display_name: "Development",
              category: "development",
              state: "ready",
              review_policy: "none",
              position: 1,
              sequence_order: 1,
            },
          ],
        }),
      });
      return;
    }

    if (path === "/api/tasks") {
      const parentTaskId = url.searchParams.get("parent_task_id");
      const body =
        parentTaskId === CLAIMED_TASK_ID
          ? { tasks: [], total: 0, stats: {}, limit: 200, offset: 0 }
          : { tasks: [claimedTask], total: 1, stats: {}, limit: 500, offset: 0 };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
      return;
    }

    if (path === `/api/tasks/${CLAIMED_TASK_ID}`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ task: claimedTask }),
      });
      return;
    }

    if (path === `/api/tasks/${CLAIMED_TASK_ID}/dependencies`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ id: CLAIMED_TASK_ID, blockers: [], blocking: [] }),
      });
      return;
    }

    if (path === "/api/sessions") {
      const hasClaimedTaskFilter =
        url.searchParams.get("task_ref_min") === String(CLAIMED_TASK_REF) &&
        url.searchParams.get("task_ref_max") === String(CLAIMED_TASK_REF) &&
        url.searchParams.getAll("task_ref_role").includes("claimed");
      const sessions = hasClaimedTaskFilter
        ? webChatSessions.filter((session) =>
            session.claimed_task_refs.includes(CLAIMED_TASK_REF),
          )
        : webChatSessions;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ sessions, total: sessions.length }),
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

    if (path === `/api/sessions/${TASK_LINKED_DB_SESSION_ID}`) {
      bumpCounter(counters, "taskLinkedSessionDetailFetches");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session: webChatSessions.find((session) => session.id === TASK_LINKED_DB_SESSION_ID),
        }),
      });
      return;
    }

    if (path === `/api/sessions/${TASK_LINKED_DB_SESSION_ID}/messages`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          messages: [
            {
              id: "task-linked-msg-1",
              role: "assistant",
              content: "Task-linked session transcript",
              timestamp: "2026-04-19T18:31:00Z",
              content_blocks: [{ type: "text", content: "Task-linked session transcript" }],
            },
          ],
          total_count: 1,
        }),
      });
      return;
    }

    if (path === `/api/sessions/${OTHER_DB_SESSION_ID}`) {
      bumpCounter(counters, "otherSessionDetailFetches");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session: webChatSessions.find((session) => session.id === OTHER_DB_SESSION_ID),
        }),
      });
      return;
    }

    if (path.startsWith(`/api/chat/${CURRENT_DB_SESSION_ID}/messages`)) {
      bumpCounter(counters, "currentMessageFetches");
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
      bumpCounter(counters, "otherMessageFetches");
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

  test("refresh restores task-linked session filters and claimed task state", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.addInitScript(
      ({
        sessionId,
        taskRef,
      }: {
        sessionId: string;
        taskRef: number;
      }) => {
        localStorage.setItem("gobby-conversation-id", sessionId);
        localStorage.setItem("gobby-db-session-id", sessionId);
        localStorage.setItem("gobby-activity-panel-pinned", "true");
        if (!localStorage.getItem("gobby-activity-panel-tab-v2")) {
          localStorage.setItem("gobby-activity-panel-tab-v2", "sessions");
        }
        localStorage.setItem(
          "gobby-sessions-filters",
          JSON.stringify({
            modes: [],
            providers: [],
            models: [],
            sessionRefMin: null,
            sessionRefMax: null,
            taskRefMin: taskRef,
            taskRefMax: taskRef,
            taskRefRoles: ["claimed"],
            datePreset: "all",
            dateCustomFrom: null,
            dateCustomTo: null,
            statuses: ["active", "paused"],
          }),
        );
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
      { sessionId: CURRENT_DB_SESSION_ID, taskRef: CLAIMED_TASK_REF },
    );
    const counters = {
      currentMessageFetches: 0,
      otherMessageFetches: 0,
      otherSessionDetailFetches: 0,
      taskLinkedSessionDetailFetches: 0,
    };
    await mockApi(page, counters);

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

    await expect(page.getByText("Persisted main chat message")).toBeVisible();
    await expect(page.getByText("Other session message")).toHaveCount(0);
    await expect(page.locator(".activity-panel-mobile-trigger")).toContainText("Sessions");
    await expect(page.locator(".session-entry")).toHaveCount(1);
    await expect(page.locator(".activity-panel-content")).toContainText(
      "#204: Task Linked Web Chat",
    );
    await expect(page.locator(".activity-panel-content")).not.toContainText(
      "Other Web Chat",
    );
    await expect(page.locator(".activity-panel-content")).not.toContainText(
      "Current Main Chat",
    );

    await page.locator(".activity-panel-mobile-trigger").click();
    await page.getByRole("menuitemradio", { name: "Tasks" }).click();

    await expect(page.locator(".activity-panel-mobile-trigger")).toContainText("Tasks");
    const taskTree = page.getByTestId("task-tree");
    const claimedTaskRow = taskTree.getByRole("treeitem", {
      name: /#14425 Verify web chat task state survives refresh: Development: In Progress.*Claimed/,
    });
    await expect(claimedTaskRow).toBeVisible();
    await expect(claimedTaskRow).toContainText("#14425");
    await expect(claimedTaskRow).toContainText("Development");
    const detail = page.locator(".activity-task-detail-shell");
    await expect(detail).toContainText("Task #14425");
    await expect(detail).toContainText("Claimed by");
    await expect(detail).toContainText(CURRENT_DB_SESSION_ID);
    await expect(detail).toContainText(/Development: In Progress.*Claimed/);

    await page.reload();

    await expect(page.getByText("Persisted main chat message")).toBeVisible();
    await expect(page.getByText("Other session message")).toHaveCount(0);
    await expect(page.locator(".activity-panel-mobile-trigger")).toContainText("Tasks");
    await expect(claimedTaskRow).toBeVisible();
    await expect(detail).toContainText(CURRENT_DB_SESSION_ID);
    await expect(detail).toContainText(/Development: In Progress.*Claimed/);
    expect(counters.currentMessageFetches).toBeGreaterThanOrEqual(2);
    expect(counters.otherMessageFetches).toBe(0);
    expect(counters.otherSessionDetailFetches).toBe(0);
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
          if (!sentPlan) {
            sentPlan = true;
            ws.send(
              JSON.stringify({
                type: "plan_pending_approval",
                conversation_id: CURRENT_DB_SESSION_ID,
                plan_content: "## Plan\n\n1. Fix restore state\n2. Add tests",
              }),
            );
          }
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

    await expect(page.getByRole("radio", { name: "YOLO", exact: true })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });
});
