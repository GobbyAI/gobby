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
  /**
   * One history window per attach, as inclusive `history-line-N` bounds. The
   * last entry serves every further attach. Windows may overlap, which is the
   * real daemon's behaviour on reconnect: it captures what the pane holds now,
   * not what this client has already painted.
   */
  historyWindows?: readonly (readonly [number, number])[];
  /** Drop the socket once the first stream is live, forcing a reconnect. */
  dropAfterFirstStream?: boolean;
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
  return historyWindowText(1, lines);
}

/** Inclusive window of numbered history lines, in the daemon's wire shape. */
function historyWindowText(from: number, to: number): string {
  return (
    Array.from(
      { length: to - from + 1 },
      (_, index) => `history-line-${from + index}`,
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
    historyWindows,
    dropAfterFirstStream = false,
  } = options;
  const activated = new Set<string>();
  // Outer state: `routeWebSocket` runs its callback per connection, and a
  // reconnect has to mint an attachment id the client has not seen before.
  let attachCount = 0;
  let dropped = false;
  const attachIndexById = new Map<string, number>();

  const windowFor = (attachIndex: number): string => {
    if (unavailable) return "";
    if (historyWindows === undefined) return historyText(historyLines);
    const bounds =
      historyWindows[Math.min(attachIndex, historyWindows.length - 1)];
    return bounds === undefined
      ? historyText(historyLines)
      : historyWindowText(bounds[0], bounds[1]);
  };

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
          }),
        );
        return;
      }

      // Attach only reserves; nothing is built and nothing is streamed yet.
      if (message.type === "terminal_attach") {
        // The first attachment keeps the plain id so the landed cases read the
        // same as before; a replacement gets one the client has never seen,
        // which is what makes it a replacement rather than a resume.
        const attachmentId =
          attachCount === 0 ? STREAM_ID : `${STREAM_ID}-${attachCount}`;
        attachIndexById.set(attachmentId, attachCount);
        attachCount += 1;
        ws.send(
          JSON.stringify({
            type: "terminal_attach_result",
            request_id: message.request_id,
            success: true,
            attachment_id: attachmentId,
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
        const text = windowFor(attachIndexById.get(streamingId) ?? 0);
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

        if (dropAfterFirstStream && !dropped) {
          dropped = true;
          // Let the first window paint, then take the socket away. The client
          // reconnects and attaches again with no memory of what it painted,
          // and the daemon answers with whatever the pane holds now — which
          // overlaps. That overlap is the whole point of the case.
          setTimeout(() => {
            if (ticker !== null) clearInterval(ticker);
            ticker = null;
            ws.close({ code: 1012, reason: "service restart" });
          }, 750);
        }
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
  // The roster filter defaults to agent-managed terminals, and these fixtures are
  // plain shells, so the list renders empty (and therefore hidden) until "All" is
  // selected. Take the same route a user does rather than making the fixtures claim
  // to be agent-managed, which is not what this spec exercises.
  await page
    .getByRole("radiogroup", { name: "Terminal sessions shown" })
    .getByRole("radio", { name: "All", exact: true })
    .click();
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

/** Numbered history lines in DOM order, which is render order. */
async function historyLineNumbers(page: Page): Promise<number[]> {
  return scrollContainer(page).evaluate((element) =>
    Array.from(element.querySelectorAll(".term-scrollback-row"))
      .map((row) => /^history-line-(\d+)$/u.exec((row.textContent ?? "").trim()))
      .filter((match): match is RegExpExecArray => match !== null)
      .map((match) => Number(match[1])),
  );
}

/** Highest `tick-N` the fixture has streamed so far, 0 before any. */
async function latestTick(page: Page): Promise<number> {
  return scrollContainer(page).evaluate((element) =>
    Array.from(element.querySelectorAll(".term-row")).reduce((highest, row) => {
      const match = /^tick-(\d+)$/u.exec((row.textContent ?? "").trim());
      return match === null ? highest : Math.max(highest, Number(match[1]));
    }, 0),
  );
}

/** Row texts intersecting the scroll viewport right now. */
async function visibleScrollbackTexts(page: Page): Promise<string[]> {
  return scrollContainer(page).evaluate((element) => {
    const view = element.getBoundingClientRect();
    return Array.from(element.querySelectorAll(".term-scrollback-row"))
      .filter((row) => {
        const rect = row.getBoundingClientRect();
        return rect.bottom > view.top && rect.top < view.bottom;
      })
      .map((row) => (row.textContent ?? "").trim())
      .filter((text) => text.length > 0);
  });
}

/**
 * Scroll the pane up with a real input gesture rather than assigning
 * scrollTop. The distinction matters: `.wterm` is a native overflow container,
 * so an assignment proves only that the property is writable, while a wheel
 * goes through the compositor the way a touch drag does and fails the same way
 * a CSS-scaled or overflow-locked container would.
 */
async function scrollUpByInput(page: Page, distance: number): Promise<void> {
  const box = await scrollContainer(page).boundingBox();
  if (box === null) throw new Error("terminal scroll container has no box");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, -distance);
}

const TIERS = [
  { label: "440x956", width: 440, height: 956 },
  { label: "932x430", width: 932, height: 430 },
  { label: "1440x900", width: 1440, height: 900 },
];

/** The tiers where the pane is driven by touch rather than a pointer. */
const MOBILE_TIER_LABELS = new Set(["440x956", "932x430"]);

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

test("bounds history per attachment across tiers", async ({ page }) => {
  // 500 lines over the client's own ceiling, and the daemon reports no
  // truncation of its own — so anything cut here was cut by this client.
  await installApiMocks(page, "dark");
  await installTerminalSocket(page, { historyLines: 2_500 });

  for (const tier of TIERS) {
    await page.setViewportSize({ width: tier.width, height: tier.height });
    await openTerminalTab(page);

    const container = scrollContainer(page);
    await expect(page.getByTestId("terminal-view")).toContainText(
      "live-line-3",
      { timeout: 20_000 },
    );
    await settledScrollback(page);

    // The ceiling, stated as the criterion states it. Counting what is
    // rendered rather than asserting a specific first line keeps this honest
    // about the renderer's own scrollback capacity, which is not ours to set.
    const rendered = await historyLineNumbers(page);
    expect(rendered.length).toBeGreaterThan(0);
    expect(rendered.length).toBeLessThanOrEqual(2_000);

    // The newest survives and the oldest is gone: the cut takes the front.
    expect(await countExactScrollbackRows(page, "history-line-2500")).toBe(1);
    expect(await countExactScrollbackRows(page, "history-line-1")).toBe(0);
    expect(await countExactScrollbackRows(page, "history-line-500")).toBe(0);

    // No marker assertion here, deliberately. The client's cut does write one,
    // but the renderer's own scrollback is smaller than the 2 000-line ceiling,
    // so a window large enough to trip the ceiling always evicts the marker
    // that sits above it. The marker itself is covered where it is observable:
    // "renders the truncation marker above restored history", on a window the
    // renderer keeps whole.

    if (!MOBILE_TIER_LABELS.has(tier.label)) continue;

    // Mobile tiers: history above the initial viewport has to be reachable by
    // dragging the pane, and live output must not yank the view back down.
    const before = await visibleScrollbackTexts(page);
    const settledTop = await container.evaluate((element) => element.scrollTop);
    await scrollUpByInput(page, 4_000);

    await expect
      .poll(() => container.evaluate((element) => element.scrollTop), {
        timeout: 10_000,
      })
      .toBeLessThan(settledTop);
    const after = await visibleScrollbackTexts(page);
    expect(after.some((text) => !before.includes(text))).toBe(true);

    // Growth is not the signal at this size: the renderer is already at its
    // scrollback capacity, so every new row evicts one and scrollHeight sits
    // still. Advancing ticks are the signal that output is arriving, and the
    // claim is that it arrives without dragging the viewport to the bottom.
    const tickBefore = await latestTick(page);
    await expect
      .poll(() => latestTick(page), { timeout: 20_000 })
      .toBeGreaterThan(tickBefore);

    const streaming = await container.evaluate((element) => ({
      scrollTop: element.scrollTop,
      maxScroll: element.scrollHeight - element.clientHeight,
    }));
    expect(streaming.scrollTop).toBeLessThan(streaming.maxScroll - 200);
  }
});

test("replacement attachment resets history without remount", async ({
  page,
}) => {
  await installApiMocks(page, "dark");
  await installTerminalSocket(page, {
    // The replacement window overlaps the first by a hundred lines, which is
    // what a real reconnect hands back: the daemon captures what the pane
    // holds now and knows nothing about what this client already painted.
    historyWindows: [
      [1, 400],
      [300, 700],
    ],
    dropAfterFirstStream: true,
  });
  await openTerminalTab(page);

  const container = scrollContainer(page);
  await expect(page.getByTestId("terminal-view")).toContainText("live-line-3", {
    timeout: 20_000,
  });
  await settledScrollback(page);
  expect(await countExactScrollbackRows(page, "history-line-400")).toBe(1);

  // Stamp React's own node, not the renderer's. React replaces its node on
  // remount, so the stamp surviving is the proof that the same component
  // instance took the replacement — nothing else distinguishes a reset from a
  // remount. Stamping `.wterm` would prove something weaker and different:
  // that element belongs to the renderer, which can rebuild it without React
  // remounting anything.
  const view = page.getByTestId("terminal-view");
  await view.evaluate((element) => {
    element.setAttribute("data-remount-probe", "first");
  });

  await expect
    .poll(() => countExactScrollbackRows(page, "history-line-700"), {
      timeout: 30_000,
    })
    .toBe(1);

  expect(await view.getAttribute("data-remount-probe")).toBe("first");

  // Each line once, and nothing from the window that was replaced.
  expect(await countExactScrollbackRows(page, "history-line-350")).toBe(1);
  expect(await countExactScrollbackRows(page, "history-line-400")).toBe(1);
  expect(await countExactScrollbackRows(page, "history-line-1")).toBe(0);
  expect(await countExactScrollbackRows(page, "history-line-299")).toBe(0);

  // In order, starting at the replacement's first line. An append would leave
  // 300..400 twice and the sequence would step backwards at the seam.
  const rendered = await historyLineNumbers(page);
  expect(rendered[0]).toBe(300);
  expect(rendered).toEqual([...rendered].sort((left, right) => left - right));
  expect(new Set(rendered).size).toBe(rendered.length);

  // Scroll position comes from the new window: the live edge of what is on
  // screen now, not a remembered offset into a buffer that no longer exists.
  const geometry = await container.evaluate((element) => ({
    scrollTop: element.scrollTop,
    scrollHeight: element.scrollHeight,
    clientHeight: element.clientHeight,
  }));
  expect(geometry.scrollTop).toBeGreaterThan(
    geometry.scrollHeight - geometry.clientHeight - 40,
  );
});

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
