import { expect, test, type Page } from "@playwright/test";

// The Changes panel must be session-scoped: it reflects the session shown in
// the main chat window (live web chat, or a watched/attached/resumed session),
// and switching the viewed session switches the Changes contents. These specs
// drive the real ChatPage -> ActivityPanel -> FileChangesTab -> useFileChanges
// path against the session-scoped /api/sessions/{id}/changes endpoint.

type SessionRecord = {
  id: string;
  externalId: string;
  changes: { path: string; status: string }[];
};

const SESSION_A: SessionRecord = {
  id: "web-session-a",
  externalId: "web-session-a-ext",
  changes: [{ path: "src/alpha.ts", status: "E" }],
};

const SESSION_B: SessionRecord = {
  id: "web-session-b",
  externalId: "web-session-b-ext",
  changes: [{ path: "src/beta.ts", status: "W" }],
};

function sessionRow(record: SessionRecord, seq: number, title: string) {
  return {
    id: record.id,
    ref: `#${seq}`,
    external_id: record.externalId,
    source: "claude",
    project_id: "proj-1",
    title,
    status: "active",
    model: "sonnet",
    message_count: 2,
    created_at: "2026-04-08T12:00:00Z",
    updated_at: "2026-04-08T12:05:00Z",
    seq_num: seq,
    summary_markdown: null,
    git_branch: "main",
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: true,
    agent_depth: 0,
    chat_mode: "plan",
    parent_session_id: null,
    session_type: "web_chat",
    terminal_context: null,
    sandbox_enabled: true,
    sandbox_policy_hash: "policy-web",
  };
}

const SESSIONS = [
  sessionRow(SESSION_A, 301, "Session A"),
  sessionRow(SESSION_B, 302, "Session B"),
];

async function setupMocks(page: Page, current: SessionRecord) {
  await page.addInitScript(
    ({ conversationId, dbSessionId }) => {
      localStorage.setItem("gobby-conversation-id", conversationId);
      localStorage.setItem("gobby-db-session-id", dbSessionId);
      localStorage.setItem("gobby-activity-panel-pinned", "true");
      localStorage.setItem("gobby-activity-panel-tab-v2", "changes");
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
    { conversationId: current.externalId, dbSessionId: current.id },
  );

  await page.routeWebSocket("**/ws", (ws) => {
    ws.onMessage((raw) => {
      let message: Record<string, unknown> | null = null;
      try {
        message = JSON.parse(String(raw));
      } catch {
        message = null;
      }
      if (message?.type === "subscribe") {
        ws.send(
          JSON.stringify({
            type: "connection_established",
            conversation_ids: [current.externalId],
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
    const json = (body: unknown) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(body),
      });

    if (path === "/api/auth/status") return json({ authenticated: true });
    if (path === "/api/config/ui-settings")
      return json({
        selectedProjectId: "proj-1",
        model: "opus",
        theme: "dark",
        defaultChatMode: "plan",
        fontSize: 16,
      });
    if (path === "/api/providers")
      return json({ providers: [{ name: "claude", available: true }] });
    if (path === "/api/voice/status")
      return json({ enabled: false, stt_available: false });
    if (path === "/api/projects" || path === "/api/files/projects")
      return json([
        {
          id: "proj-1",
          name: "project-one",
          display_name: "Project One",
          checkout: {
            machine_id: "machine-1",
            root_path: "/tmp/project-one",
          },
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
      ]);
    if (path === "/api/agents/running") return json({ agents: [] });
    if (path === "/api/sessions")
      return json({ sessions: SESSIONS, total: SESSIONS.length });

    // Session-scoped Changes endpoint — the heart of this spec.
    const changesMatch = path.match(/^\/api\/sessions\/([^/]+)\/changes$/);
    if (changesMatch) {
      const sid = changesMatch[1];
      const record = [SESSION_A, SESSION_B].find((s) => s.id === sid);
      return json({ files: record?.changes ?? [], isolation: "none" });
    }
    if (/^\/api\/sessions\/[^/]+\/changes\/diff$/.test(path)) {
      return json({ diff: "", path: url.searchParams.get("path") ?? "" });
    }

    const sessionMatch = path.match(/^\/api\/sessions\/([^/]+)$/);
    if (sessionMatch) {
      const sid = sessionMatch[1];
      return json({ session: SESSIONS.find((s) => s.id === sid) ?? null });
    }
    if (/^\/api\/chat\/[^/]+\/messages$/.test(path))
      return json({ messages: [] });

    return json({});
  });
}

test("Changes panel shows the current session's working-tree changes", async ({
  page,
}) => {
  await setupMocks(page, SESSION_A);
  await page.goto("/");

  await expect(page.getByText("alpha.ts")).toBeVisible();
  await expect(page.getByText("beta.ts")).toHaveCount(0);
});

test("Changes panel contents switch when the viewed session switches", async ({
  page,
}) => {
  // A different viewed session resolves to a different working tree, so the
  // Changes panel must surface that session's files instead.
  await setupMocks(page, SESSION_B);
  await page.goto("/");

  await expect(page.getByText("beta.ts")).toBeVisible();
  await expect(page.getByText("alpha.ts")).toHaveCount(0);
});
