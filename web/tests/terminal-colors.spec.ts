import { expect, test, type Page } from "@playwright/test";

interface TmuxSessionFixture {
  name: string;
  socket: string;
  pane_pid: number;
  pane_dead: boolean;
  pane_title: string;
  window_name: string;
  session_title: string;
  gobby_session_id: string | null;
  agent_managed: boolean;
  agent_run_id: string | null;
  attached_bridge: string | null;
}

interface TerminalHarness {
  messages: Array<Record<string, unknown>>;
}

const ANSI_OUTPUT =
  [
    "\x1b[1;37m=== ANSI Color Test ===\x1b[0m",
    "\x1b[31m[31] Red\x1b[0m",
    "\x1b[32m[32] Green\x1b[0m",
    "Default foreground text for comparison",
  ].join("\r\n") + "\r\n";

const MOCK_SESSIONS: TmuxSessionFixture[] = [
  {
    name: "test-session",
    socket: "default",
    pane_pid: 12345,
    pane_dead: false,
    pane_title: "Terminal color fixture",
    window_name: "colors",
    session_title: "Terminal color fixture",
    gobby_session_id: null,
    agent_managed: false,
    agent_run_id: null,
    attached_bridge: null,
  },
  {
    name: "second-session",
    socket: "gobby",
    pane_pid: 23456,
    pane_dead: false,
    pane_title: "Second fixture",
    window_name: "second",
    session_title: "Second fixture",
    gobby_session_id: null,
    agent_managed: false,
    agent_run_id: null,
    attached_bridge: null,
  },
];

const STREAM_IDS: Record<string, string> = {
  "test-session": "stream-test-session",
  "second-session": "stream-second-session",
};

const OUTPUT_BY_STREAM: Record<string, string> = {
  "stream-test-session": ANSI_OUTPUT,
  "stream-second-session": "Second session output\r\n",
};

async function installApiMocks(page: Page): Promise<void> {
  await page.addInitScript(() => {
    localStorage.removeItem("gobby-conversation-id");
    localStorage.removeItem("gobby-db-session-id");
    localStorage.setItem("gobby-activity-panel-layout", "chat");
    localStorage.setItem("gobby-activity-panel-tab-v2", "sessions");
    localStorage.setItem(
      "gobby-settings",
      JSON.stringify({
        model: "opus",
        fontSize: 16,
        theme: "dark",
        defaultChatMode: "plan",
      }),
    );
  });

  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(body),
      });

    if (path === "/api/auth/status") {
      return json({ authenticated: true });
    }
    if (path === "/api/config/ui-settings") {
      return json({
        selectedProjectId: "project-terminal-test",
        model: "opus",
        theme: "dark",
        defaultChatMode: "plan",
        fontSize: 16,
      });
    }
    if (path === "/api/providers") {
      return json({ providers: [{ name: "claude", available: true }] });
    }
    if (path === "/api/providers/models") {
      return json({ providers: [] });
    }
    if (path === "/api/voice/status") {
      return json({ enabled: false, stt_available: false });
    }
    if (path === "/api/projects" || path === "/api/files/projects") {
      return json([
        {
          id: "project-terminal-test",
          name: "terminal-test",
          display_name: "Terminal Test",
          repo_path: "/tmp/terminal-test",
          github_url: null,
          github_repo: null,
          linear_team_id: null,
          approval_rules: [],
          created_at: "2026-07-22T00:00:00Z",
          updated_at: "2026-07-22T00:00:00Z",
          session_count: 0,
          open_task_count: 0,
          last_activity_at: null,
        },
      ]);
    }
    if (path === "/api/agents/running") {
      return json({ agents: [] });
    }
    if (path === "/api/sessions") {
      return json({ sessions: [], total: 0 });
    }
    if (path === "/api/tasks") {
      return json({ tasks: [], total: 0, stats: {}, limit: 200, offset: 0 });
    }

    return json({});
  });
}

async function installTerminalSocket(page: Page): Promise<TerminalHarness> {
  const messages: Array<Record<string, unknown>> = [];
  const outputSent = new Set<string>();

  await page.routeWebSocket("**/ws", (ws) => {
    ws.onMessage((raw) => {
      let message: Record<string, unknown>;
      try {
        message = JSON.parse(String(raw)) as Record<string, unknown>;
      } catch {
        return;
      }
      messages.push(message);

      if (message.type === "subscribe") {
        ws.send(
          JSON.stringify({
            type: "connection_established",
            conversation_ids: [],
          }),
        );
        ws.send(
          JSON.stringify({
            type: "subscribe_success",
            events: message.events ?? [],
          }),
        );
        return;
      }

      if (message.type === "terminal_list") {
        ws.send(
          JSON.stringify({
            type: "terminal_list",
            sessions: MOCK_SESSIONS,
            live_cli_session_ids: [],
          }),
        );
        return;
      }

      if (message.type === "terminal_attach") {
        const sessionName = String(message.session_name);
        ws.send(
          JSON.stringify({
            type: "terminal_attach_result",
            request_id: message.request_id,
            success: true,
            streaming_id: STREAM_IDS[sessionName],
          }),
        );
        return;
      }

      if (message.type === "terminal_detach") {
        ws.send(
          JSON.stringify({
            type: "terminal_detach_result",
            request_id: message.request_id,
            success: true,
          }),
        );
        return;
      }

      if (message.type === "terminal_resize") {
        const streamingId = String(message.streaming_id);
        if (!outputSent.has(streamingId)) {
          outputSent.add(streamingId);
          ws.send(
            JSON.stringify({
              type: "terminal_output",
              run_id: streamingId,
              data:
                OUTPUT_BY_STREAM[streamingId] ?? "Unknown terminal output\r\n",
            }),
          );
        }
      }
    });
  });

  return { messages };
}

async function openTerminalTab(page: Page): Promise<void> {
  await page.goto("/");
  await page.getByRole("button", { name: "Show activity panel" }).click();

  const tabTrigger = page.locator(".activity-panel-mobile-trigger");
  await expect(tabTrigger).toContainText("Sessions");
  await tabTrigger.click();
  await page
    .locator(".activity-panel-mobile-menu")
    .getByRole("button", { name: "Terminal", exact: true })
    .click();

  await expect(tabTrigger).toContainText("Terminal");
  await expect(
    page.getByRole("combobox", { name: "Terminal session" }),
  ).toBeVisible({
    timeout: 15_000,
  });
}

async function chooseTerminalSession(page: Page, name: string): Promise<void> {
  const picker = page.getByRole("combobox", { name: "Terminal session" });
  await picker.click();
  await page.getByRole("option", { name, exact: true }).click();
  await expect(picker).toContainText(name);
}

function messagesOfType(
  harness: TerminalHarness,
  type: string,
): Array<Record<string, unknown>> {
  return harness.messages.filter((message) => message.type === type);
}

test.beforeEach(async ({ page }) => {
  await installApiMocks(page);
});

test("renders ANSI output and row-grid styling in the activity terminal", async ({
  page,
}) => {
  await installTerminalSocket(page);
  await openTerminalTab(page);
  await chooseTerminalSession(page, "test-session");

  const terminal = page.getByTestId("terminal-view");
  await expect(terminal).toContainText(
    "Default foreground text for comparison",
  );

  const renderedStyle = await terminal.locator(".wterm").evaluate((element) => {
    const row = Array.from(
      element.querySelectorAll<HTMLElement>(".term-row"),
    ).find((item) => item.textContent?.includes("[31] Red"));
    const redRun = Array.from(
      element.querySelectorAll<HTMLElement>(".term-row > span"),
    ).find((item) => item.textContent?.includes("[31] Red"));
    const defaultRun = Array.from(
      element.querySelectorAll<HTMLElement>(".term-row > span"),
    ).find((item) => item.textContent?.includes("Default foreground text"));
    if (!row || !redRun || !defaultRun) {
      throw new Error("Expected ANSI and default terminal rows");
    }

    const terminalStyle = getComputedStyle(element);
    const rowStyle = getComputedStyle(row);
    return {
      defaultColor: getComputedStyle(defaultRun).color,
      redColor: getComputedStyle(redRun).color,
      rowHeight: rowStyle.height,
      rowLineHeight: rowStyle.lineHeight,
      rowHeightToken: terminalStyle
        .getPropertyValue("--term-row-height")
        .trim(),
    };
  });

  expect(renderedStyle.rowHeightToken).toMatch(/^\d+px$/);
  expect(renderedStyle.rowHeight).toBe(renderedStyle.rowHeightToken);
  expect(renderedStyle.rowLineHeight).toBe(renderedStyle.rowHeightToken);
  expect(renderedStyle.redColor).toBe("rgb(204, 102, 102)");
  expect(renderedStyle.redColor).not.toBe(renderedStyle.defaultColor);
});

test("forwards composer input and detaches before switching terminal sessions", async ({
  page,
}) => {
  const harness = await installTerminalSocket(page);
  await openTerminalTab(page);

  const terminal = page.getByTestId("terminal-view");
  await expect(terminal).toContainText(
    "Default foreground text for comparison",
  );
  await expect
    .poll(() => messagesOfType(harness, "terminal_attach"))
    .toContainEqual(
      expect.objectContaining({
        session_name: "test-session",
        socket: "default",
      }),
    );

  await page.getByRole("button", { name: "Open terminal composer" }).click();
  await page.getByRole("textbox", { name: "Terminal input" }).fill("status");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await page.getByRole("button", { name: "Esc", exact: true }).click();
  await page.getByRole("button", { name: "Ctrl+C", exact: true }).click();

  await expect
    .poll(() =>
      messagesOfType(harness, "terminal_input")
        .filter((message) =>
          ["status\r", "\x1b", "\x03"].includes(String(message.data)),
        )
        .map(({ run_id, data }) => ({ run_id, data })),
    )
    .toEqual([
      { run_id: STREAM_IDS["test-session"], data: "status\r" },
      { run_id: STREAM_IDS["test-session"], data: "\x1b" },
      { run_id: STREAM_IDS["test-session"], data: "\x03" },
    ]);

  await chooseTerminalSession(page, "second-session");
  await expect(terminal).toContainText("Second session output");

  await expect
    .poll(() => messagesOfType(harness, "terminal_detach"))
    .toContainEqual(
      expect.objectContaining({
        streaming_id: STREAM_IDS["test-session"],
      }),
    );
  await expect
    .poll(() => messagesOfType(harness, "terminal_attach"))
    .toContainEqual(
      expect.objectContaining({
        session_name: "second-session",
        socket: "gobby",
      }),
    );
  const firstDetachIndex = harness.messages.findIndex(
    (message) =>
      message.type === "terminal_detach" &&
      message.streaming_id === STREAM_IDS["test-session"],
  );
  const secondAttachIndex = harness.messages.findIndex(
    (message) =>
      message.type === "terminal_attach" &&
      message.session_name === "second-session",
  );

  expect(firstDetachIndex).toBeGreaterThan(-1);
  expect(secondAttachIndex).toBeGreaterThan(firstDetachIndex);
});

test("keeps streamed output visible when Ghostty WASM falls back", async ({
  page,
}) => {
  await page.route("**/wasm/ghostty-vt.wasm", (route) => route.abort());
  await installTerminalSocket(page);
  await openTerminalTab(page);

  const terminal = page.getByTestId("terminal-view");
  await expect(
    page.getByText("Reduced terminal fidelity", { exact: true }),
  ).toBeVisible();
  await expect(terminal).toContainText(
    "Default foreground text for comparison",
  );
  await expect(terminal.locator(".term-row")).not.toHaveCount(0);
});
