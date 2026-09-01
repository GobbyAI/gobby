import { expect, test, type Page } from "@playwright/test";

interface TmuxSessionFixture {
  terminal_id: string;
  name: string;
  socket: string;
  pane_pid: number;
  pane_dead: boolean;
  pane_title: string;
  pane_command: string | null;
  pane_path: string | null;
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
    terminal_id: "terminal-test-session",
    name: "test-session",
    socket: "default",
    pane_pid: 12345,
    pane_dead: false,
    pane_title: "Terminal color fixture",
    pane_command: null,
    pane_path: null,
    window_name: "colors",
    session_title: "Terminal color fixture",
    gobby_session_id: null,
    agent_managed: false,
    agent_run_id: null,
    attached_bridge: null,
  },
  {
    terminal_id: "terminal-second-session",
    name: "second-session",
    socket: "gobby",
    pane_pid: 23456,
    pane_dead: false,
    pane_title: "Second fixture",
    pane_command: null,
    pane_path: null,
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

  // Predicate, not a glob: "**/api/**" also matches vite's own module
  // requests for src/api/*, which stubs them as JSON and the app never boots.
  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    async (route) => {
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
            checkout: {
              machine_id: "machine-1",
              root_path: "/tmp/terminal-test",
            },
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
    },
  );
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
            request_id: message.request_id,
            next_cursor: null,
            items: MOCK_SESSIONS,
            live_cli_session_ids: [],
          }),
        );
        return;
      }

      if (message.type === "terminal_attach") {
        const terminalId = String(message.terminal_id);
        const session = MOCK_SESSIONS.find(
          (candidate) => candidate.terminal_id === terminalId,
        );
        if (!session) return;
        ws.send(
          JSON.stringify({
            type: "terminal_attach_result",
            request_id: message.request_id,
            success: true,
            attachment_id: STREAM_IDS[session.name],
            terminal_id: terminalId,
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
        const streamingId = String(message.attachment_id);
        if (!outputSent.has(streamingId)) {
          outputSent.add(streamingId);
          ws.send(
            JSON.stringify({
              type: "terminal_output",
              attachment_id: streamingId,
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
    page.getByRole("list", { name: "Terminal sessions" }),
  ).toBeVisible({ timeout: 15_000 });
}

async function chooseTerminalSession(page: Page, name: string): Promise<void> {
  // Rows, not a select: TerminalSessionList renders a role="list" of
  // aria-pressed attach buttons.
  const row = page.getByRole("button", { name: `Attach ${name}`, exact: true });
  await row.click();
  await expect(row).toHaveAttribute("aria-pressed", "true");
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

  // Resolve the token rather than hard-coding its current value: the palette
  // is allowed to move, the ANSI-red -> --color-error mapping is not.
  const expectedRed = await page.evaluate(() => {
    const probe = document.createElement("span");
    probe.style.color = "var(--color-error)";
    document.body.appendChild(probe);
    const color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  });

  expect(renderedStyle.rowHeightToken).toMatch(/^\d+px$/);
  expect(renderedStyle.rowHeight).toBe(renderedStyle.rowHeightToken);
  expect(renderedStyle.rowLineHeight).toBe(renderedStyle.rowHeightToken);
  expect(renderedStyle.redColor).toBe(expectedRed);
  expect(renderedStyle.redColor).not.toBe(renderedStyle.defaultColor);
});

test("forwards terminal input and detaches before switching terminal sessions", async ({
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
        terminal_id: "terminal-test-session",
      }),
    );

  // Typing goes straight into the focused terminal; the quick-keys bar covers
  // only the keys an on-screen keyboard cannot produce.
  await terminal.locator(".wterm").click();
  await page.keyboard.type("status");
  await page.keyboard.press("Enter");
  await page.getByRole("button", { name: "Esc", exact: true }).click();
  await page.getByRole("button", { name: "Ctrl+C", exact: true }).click();

  await expect
    .poll(() =>
      messagesOfType(harness, "terminal_input")
        .map((message) => String(message.data))
        .join(""),
    )
    .toBe("status\r\x1b\x03");
  expect([
    ...new Set(
      messagesOfType(harness, "terminal_input").map(
        (message) => message.attachment_id,
      ),
    ),
  ]).toEqual([STREAM_IDS["test-session"]]);

  await chooseTerminalSession(page, "second-session");
  await expect(terminal).toContainText("Second session output");

  await expect
    .poll(() => messagesOfType(harness, "terminal_detach"))
    .toContainEqual(
      expect.objectContaining({
        attachment_id: STREAM_IDS["test-session"],
      }),
    );
  await expect
    .poll(() => messagesOfType(harness, "terminal_attach"))
    .toContainEqual(
      expect.objectContaining({
        terminal_id: "terminal-second-session",
      }),
    );
  const firstDetachIndex = harness.messages.findIndex(
    (message) =>
      message.type === "terminal_detach" &&
      message.attachment_id === STREAM_IDS["test-session"],
  );
  const secondAttachIndex = harness.messages.findIndex(
    (message) =>
      message.type === "terminal_attach" &&
      message.terminal_id === "terminal-second-session",
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
