/**
 * Terminal scrollback restore on the shipped tmux path.
 *
 * The daemon acknowledges `terminal_attach` as a reservation and does the real
 * work on the first `terminal_resize`: build the bridge at the client's geometry,
 * send one bounded `terminal_attach_history` frame, then stream. The fake
 * socket below implements exactly that handshake.
 *
 * Project selection is by title. The default `chromium` project excludes
 * `@style-capture`, and `style-capture-coarse` selects `--coarse--`, so a
 * coarse-pointer cell must carry BOTH tags — a title with only `--coarse--`
 * would also run in `chromium` under a fine pointer, which defeats the point.
 */
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

interface SocketOptions {
  truncated?: boolean;
  unavailable?: boolean;
  historyLines?: number;
}

const SESSION_NAME = "history-session";
const STREAM_ID = "stream-history-session";

const MOCK_SESSIONS: TmuxSessionFixture[] = [
  {
    terminal_id: "terminal-history-session",
    name: SESSION_NAME,
    socket: "default",
    pane_pid: 12345,
    pane_dead: false,
    pane_title: "History fixture",
    pane_command: null,
    pane_path: null,
    window_name: "history",
    session_title: "History fixture",
    gobby_session_id: null,
    agent_managed: false,
    agent_run_id: null,
    attached_bridge: null,
  },
];

const LIVE_OUTPUT =
  ["live-line-1", "live-line-2", "live-line-3"].join("\r\n") + "\r\n";

function historyText(lines: number): string {
  return (
    Array.from(
      { length: lines },
      (_, index) => `history-line-${index + 1}`,
    ).join("\r\n") + "\x1b[0m"
  );
}

async function installApiMocks(page: Page, theme: "dark" | "light") {
  await page.addInitScript((activeTheme: string) => {
    localStorage.removeItem("gobby-conversation-id");
    localStorage.removeItem("gobby-db-session-id");
    localStorage.setItem("gobby-activity-panel-layout", "chat");
    localStorage.setItem("gobby-activity-panel-tab-v2", "sessions");
    localStorage.setItem(
      "gobby-settings",
      JSON.stringify({
        model: "opus",
        fontSize: 16,
        theme: activeTheme,
        defaultChatMode: "plan",
      }),
    );
  }, theme);

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

      if (path === "/api/auth/status") return json({ authenticated: true });
      if (path === "/api/config/ui-settings") {
        return json({
          selectedProjectId: "project-terminal-history",
          model: "opus",
          theme,
          defaultChatMode: "plan",
          fontSize: 16,
        });
      }
      if (path === "/api/providers") {
        return json({ providers: [{ name: "claude", available: true }] });
      }
      if (path === "/api/providers/models") return json({ providers: [] });
      if (path === "/api/voice/status") {
        return json({ enabled: false, stt_available: false });
      }
      if (path === "/api/projects" || path === "/api/files/projects") {
        return json([
          {
            id: "project-terminal-history",
            name: "terminal-history",
            display_name: "Terminal History",
            checkout: {
              machine_id: "machine-1",
              root_path: "/tmp/terminal-history",
            },
            github_url: null,
            github_repo: null,
            linear_team_id: null,
            approval_rules: [],
            created_at: "2026-08-22T00:00:00Z",
            updated_at: "2026-08-22T00:00:00Z",
            session_count: 0,
            open_task_count: 0,
            last_activity_at: null,
          },
        ]);
      }
      if (path === "/api/agents/running") return json({ agents: [] });
      if (path === "/api/sessions") return json({ sessions: [], total: 0 });
      if (path === "/api/tasks") {
        return json({ tasks: [], total: 0, stats: {}, limit: 200, offset: 0 });
      }
      return json({});
    },
  );
}

/** Force the wterm built-in core by starving the Ghostty wasm fetch. */
async function starveGhosttyWasm(page: Page): Promise<void> {
  await page.route("**/wasm/ghostty-vt.wasm", (route) =>
    route.fulfill({ status: 404, body: "" }),
  );
}

async function installTerminalSocket(
  page: Page,
  options: SocketOptions = {},
): Promise<void> {
  const {
    truncated = false,
    unavailable = false,
    historyLines = 400,
  } = options;
  const activated = new Set<string>();

  await page.routeWebSocket("**/ws", (ws) => {
    let ticker: ReturnType<typeof setInterval> | null = null;
    ws.onClose(() => {
      if (ticker !== null) clearInterval(ticker);
      ticker = null;
    });
    ws.onMessage((raw) => {
      let message: Record<string, unknown>;
      try {
        message = JSON.parse(String(raw)) as Record<string, unknown>;
      } catch {
        return;
      }

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

      // Attach only reserves; nothing is built and nothing is streamed yet.
      if (message.type === "terminal_attach") {
        ws.send(
          JSON.stringify({
            type: "terminal_attach_result",
            request_id: message.request_id,
            success: true,
            attachment_id: STREAM_ID,
            terminal_id: message.terminal_id,
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

      // The first resize is the activation point: history, then the stream.
      if (message.type === "terminal_resize") {
        const streamingId = String(message.attachment_id);
        if (activated.has(streamingId)) return;
        activated.add(streamingId);
        const text = unavailable ? "" : historyText(historyLines);
        ws.send(
          JSON.stringify({
            type: "terminal_attach_history",
            attachment_id: streamingId,
            text,
            truncated,
            unavailable,
            dropped_bytes: truncated ? 4096 : 0,
            total_bytes: text.length,
          }),
        );
        ws.send(
          JSON.stringify({
            type: "terminal_output",
            attachment_id: streamingId,
            data: LIVE_OUTPUT,
          }),
        );
        // Keep streaming afterwards. Typing cannot be used to produce output
        // for the follow-live-edge assertions: wterm scrolls to the bottom on
        // any keystroke, which is correct terminal behavior and would mask
        // exactly the snap this test is looking for.
        let tick = 0;
        ticker = setInterval(() => {
          tick += 1;
          ws.send(
            JSON.stringify({
              type: "terminal_output",
              attachment_id: streamingId,
              data: `tick-${tick}\r\n`,
            }),
          );
        }, 250);
        return;
      }
    });
  });
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

function scrollContainer(page: Page) {
  return page.getByTestId("terminal-view").locator(".wterm");
}

/** Terminal rows are padded to the full grid width, so compare trimmed text. */
async function countExactScrollbackRows(
  page: Page,
  text: string,
): Promise<number> {
  return scrollContainer(page).evaluate(
    (element, expected) =>
      Array.from(element.querySelectorAll(".term-scrollback-row")).filter(
        (row) => (row.textContent ?? "").trim() === expected,
      ).length,
    text,
  );
}

async function settledScrollback(page: Page): Promise<number> {
  const container = scrollContainer(page);
  await expect
    .poll(
      async () =>
        container.evaluate(
          (element) => element.querySelectorAll(".term-scrollback-row").length,
        ),
      { timeout: 20_000 },
    )
    .toBeGreaterThan(50);
  return container.evaluate(
    (element) => element.querySelectorAll(".term-scrollback-row").length,
  );
}

const TIERS = [
  { label: "440x956", width: 440, height: 956 },
  { label: "932x430", width: 932, height: 430 },
  { label: "1440x900", width: 1440, height: 900 },
];

for (const tier of TIERS) {
  test.describe(`terminal history at ${tier.label}`, () => {
    test.use({ viewport: { width: tier.width, height: tier.height } });

    test(`restores scrollback and holds position while output streams (${tier.label})`, async ({
      page,
    }) => {
      await installApiMocks(page, "dark");
      await installTerminalSocket(page);
      await openTerminalTab(page);

      const terminal = page.getByTestId("terminal-view");
      await expect(terminal).toContainText("live-line-3", { timeout: 20_000 });

      const container = scrollContainer(page);
      await settledScrollback(page);

      // History that predates this attach is reachable, which is the whole point.
      expect(await countExactScrollbackRows(page, "history-line-1")).toBe(1);
      expect(await countExactScrollbackRows(page, "history-line-400")).toBe(1);

      const overflow = await container.evaluate((element) => ({
        scrollHeight: element.scrollHeight,
        clientHeight: element.clientHeight,
      }));
      expect(overflow.scrollHeight).toBeGreaterThan(overflow.clientHeight);

      // Park the viewport in history while the stream keeps producing rows.
      const parked = await container.evaluate((element) => {
        element.scrollTop = 0;
        return {
          scrollTop: element.scrollTop,
          scrollHeight: element.scrollHeight,
        };
      });

      // Wait for real growth, so the assertion below has something to prove.
      await expect
        .poll(() => container.evaluate((element) => element.scrollHeight), {
          timeout: 20_000,
        })
        .toBeGreaterThan(parked.scrollHeight + 100);

      // Output arriving while scrolled up must not snap the viewport.
      const afterOutput = await container.evaluate((element) => ({
        scrollTop: element.scrollTop,
        maxScroll: element.scrollHeight - element.clientHeight,
      }));
      expect(afterOutput.scrollTop).toBeLessThan(afterOutput.maxScroll - 200);
      expect(afterOutput.scrollTop).toBe(parked.scrollTop);

      const jump = page.getByRole("button", {
        name: "Jump to newest terminal output",
      });
      await expect(jump).toBeVisible();
      await jump.click();

      await expect(jump).toBeHidden();
      const resumed = await container.evaluate((element) => ({
        scrollTop: element.scrollTop,
        scrollHeight: element.scrollHeight,
        clientHeight: element.clientHeight,
      }));
      // wterm parks the live edge on a row boundary, so allow one row of slack.
      expect(resumed.scrollTop).toBeGreaterThan(
        resumed.scrollHeight - resumed.clientHeight - 40,
      );
    });
  });
}

test("renders the truncation marker above restored history", async ({
  page,
}) => {
  await installApiMocks(page, "dark");
  await installTerminalSocket(page, { truncated: true, historyLines: 120 });
  await openTerminalTab(page);

  const container = scrollContainer(page);
  await expect(page.getByTestId("terminal-view")).toContainText("live-line-3", {
    timeout: 20_000,
  });

  const rows = container.locator(".term-scrollback-row");
  await expect
    .poll(async () => (await rows.first().textContent()) ?? "", {
      timeout: 20_000,
    })
    .toContain("earlier output not shown");
  await expect(rows.nth(1)).toContainText("history-line-1");
});

test("degrades visibly when the daemon could not capture history", async ({
  page,
}) => {
  await installApiMocks(page, "dark");
  await installTerminalSocket(page, { unavailable: true });
  await openTerminalTab(page);

  const terminal = page.getByTestId("terminal-view");
  // Losing scrollback must not cost the user a working terminal.
  await expect(terminal).toContainText("live-line-3", { timeout: 20_000 });
  await expect(terminal).toContainText("history unavailable");
});

test("restores history on the built-in wterm core when Ghostty is unavailable", async ({
  page,
}) => {
  await installApiMocks(page, "dark");
  await starveGhosttyWasm(page);
  await installTerminalSocket(page);
  await openTerminalTab(page);

  const terminal = page.getByTestId("terminal-view");
  await expect(terminal).toContainText("Reduced terminal fidelity", {
    timeout: 20_000,
  });
  await expect(terminal).toContainText("live-line-3", { timeout: 20_000 });
  await settledScrollback(page);
  expect(await countExactScrollbackRows(page, "history-line-1")).toBe(1);
});

test("restores history in light mode", async ({ page }) => {
  await installApiMocks(page, "light");
  await installTerminalSocket(page, { truncated: true, historyLines: 120 });
  await openTerminalTab(page);

  const terminal = page.getByTestId("terminal-view");
  await expect(terminal).toContainText("live-line-3", { timeout: 20_000 });

  const marker = scrollContainer(page)
    .locator(".term-scrollback-row", { hasText: "earlier output not shown" })
    .first();
  await expect(marker).toBeVisible();

  // Markers are plain text on the terminal foreground, so their contrast is
  // the theme's own body-text contrast rather than an unproven faint SGR.
  const colors = await marker.evaluate((element) => {
    const container = element.closest(".wterm") as HTMLElement | null;
    const body = container?.querySelector<HTMLElement>(
      ".term-scrollback-row:not(:first-child)",
    );
    return {
      markerColor: getComputedStyle(element).color,
      bodyColor: body ? getComputedStyle(body).color : "",
      opacity: getComputedStyle(element).opacity,
    };
  });
  expect(colors.markerColor).toBe(colors.bodyColor);
  expect(colors.opacity).toBe("1");
});

test("keeps the jump control on a coarse touch target @style-capture --coarse--", async ({
  page,
}) => {
  await installApiMocks(page, "dark");
  await installTerminalSocket(page);
  await openTerminalTab(page);

  const container = scrollContainer(page);
  await expect(page.getByTestId("terminal-view")).toContainText("live-line-3", {
    timeout: 20_000,
  });
  await settledScrollback(page);

  await container.evaluate((element) => {
    element.scrollTop = 0;
  });

  const jump = page.getByRole("button", {
    name: "Jump to newest terminal output",
  });
  await expect(jump).toBeVisible();

  const box = await jump.boundingBox();
  expect(box).not.toBeNull();
  expect(box?.width ?? 0).toBeGreaterThanOrEqual(44);
  expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);

  // The control is a sibling of the live region, never a child of it.
  expect(
    await jump.evaluate((element) => element.closest('[role="log"]') === null),
  ).toBe(true);
});
