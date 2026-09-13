/**
 * Writer-lease acceptance for the web terminal (plan `native-runtime-completion`
 * §6.1, items 6.1.3 and 6.1.6).
 *
 * The daemon grants write authority only through `terminal_take_control` —
 * every attach is observe-only — so the fake socket below implements that
 * exchange on top of the attach handshake `terminal-history-scroll.spec.ts`
 * already models: attach reserves, the first resize activates, and every write
 * is answered by exactly one `terminal_write_outcome` carrying the
 * `client_write_seq` it was sent with.
 *
 * Bracketing itself is the daemon's job — gclient and the browser never wrap a
 * paste in `\x1b[200~…\x1b[201~`. What this spec pins on the client side is the
 * property that makes bracketing possible at all: one `terminal_paste` frame
 * carrying the whole multi-line clipboard verbatim, rather than a burst of
 * `terminal_input` keystrokes whose newlines would each submit.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";

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

type Frame = Record<string, unknown>;

/** How the fake answers the next `terminal_take_control`. */
type ControlMode = "grant" | "refuse";
/** How the fake settles the next write. */
type WriteMode = "delivered" | "indeterminate";

const SESSION_NAME = "lease-session";
const TERMINAL_ID = "terminal-lease-session";
const STREAM_ID = "stream-lease-session";
const OTHER_HOLDER = "attachment-another-session";
const DAEMON_EPOCH = "00000000-0000-4000-8000-000000000000";
const INDETERMINATE_REASON = "indeterminate_backend";

const MOCK_SESSIONS: TmuxSessionFixture[] = [
  {
    terminal_id: TERMINAL_ID,
    name: SESSION_NAME,
    socket: "default",
    pane_pid: 24680,
    pane_dead: false,
    pane_title: "Lease fixture",
    pane_command: null,
    pane_path: null,
    window_name: "lease",
    session_title: "Lease fixture",
    gobby_session_id: null,
    agent_managed: false,
    agent_run_id: null,
    attached_bridge: null,
  },
];

// Enough rows to leave real scrollback behind the live edge — parking the
// viewport in history is what puts the jump-to-bottom control on screen.
const HISTORY_TEXT =
  Array.from({ length: 400 }, (_, index) => `history-line-${index + 1}`).join(
    "\r\n",
  ) + "\x1b[0m";

const LIVE_OUTPUT =
  ["live-line-1", "live-line-2", "live-line-3"].join("\r\n") + "\r\n";

// Multi-line on purpose: a clipboard delivered as keystrokes would submit each
// line, which is exactly what one bracketed paste prevents.
const PASTE_TEXT = "echo first\necho second\necho third";

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

  // Predicate, not a glob: "**/api/**" also matches vite's own module requests
  // for src/api/*, which stubs them as JSON and the app never boots.
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
          selectedProjectId: "project-terminal-lease",
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
            id: "project-terminal-lease",
            name: "terminal-lease",
            display_name: "Terminal Lease",
            checkout: {
              machine_id: "machine-1",
              root_path: "/tmp/terminal-lease",
            },
            github_url: null,
            github_repo: null,
            linear_team_id: null,
            approval_rules: [],
            created_at: "2026-09-11T00:00:00Z",
            updated_at: "2026-09-11T00:00:00Z",
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

interface TerminalSocket {
  /** Everything the client has sent so far, of one wire type, in order. */
  sent: (type: string) => Frame[];
  /** Another session takes the lease: the generation moves and we are told. */
  displace: () => void;
  answerControlWith: (mode: ControlMode) => void;
  answerWritesWith: (mode: WriteMode) => void;
}

async function installTerminalSocket(page: Page): Promise<TerminalSocket> {
  const received: Frame[] = [];
  const activated = new Set<string>();
  // A holder rather than bare `let`s: the route callback and the returned
  // controller both mutate this long after this function has returned.
  const state = {
    send: null as ((frame: Frame) => void) | null,
    control: "grant" as ControlMode,
    write: "delivered" as WriteMode,
    generation: 0,
  };

  await page.routeWebSocket("**/ws", (ws) => {
    const send = (frame: Frame) => ws.send(JSON.stringify(frame));
    state.send = send;
    ws.onClose(() => {
      state.send = null;
    });
    ws.onMessage((raw) => {
      let message: Frame;
      try {
        message = JSON.parse(String(raw)) as Frame;
      } catch {
        return;
      }
      received.push(message);

      if (message.type === "subscribe") {
        send({ type: "connection_established", conversation_ids: [] });
        send({ type: "subscribe_success", events: message.events ?? [] });
        return;
      }

      if (message.type === "terminal_list") {
        send({
          type: "terminal_list",
          request_id: message.request_id,
          next_cursor: null,
          items: MOCK_SESSIONS,
        });
        return;
      }

      // Attach only reserves, and it is observe-only: it publishes no lease
      // generation of its own and grants no write authority.
      if (message.type === "terminal_attach") {
        send({
          type: "terminal_attach_result",
          request_id: message.request_id,
          success: true,
          attachment_id: STREAM_ID,
          terminal_id: message.terminal_id,
          lease_generation: 0,
        });
        return;
      }

      if (message.type === "terminal_detach") {
        send({
          type: "terminal_detach_result",
          request_id: message.request_id,
          success: true,
        });
        return;
      }

      // The first resize is the activation point: history, then the stream.
      if (message.type === "terminal_resize") {
        const attachmentId = String(message.attachment_id);
        if (activated.has(attachmentId)) return;
        activated.add(attachmentId);
        send({
          type: "terminal_attach_history",
          attachment_id: attachmentId,
          terminal_id: TERMINAL_ID,
          text: HISTORY_TEXT,
          truncated: false,
          unavailable: false,
          dropped_bytes: 0,
          total_bytes: HISTORY_TEXT.length,
        });
        send({
          type: "terminal_output",
          attachment_id: attachmentId,
          terminal_id: TERMINAL_ID,
          data: LIVE_OUTPUT,
        });
        return;
      }

      // The sole grant path. A held terminal refuses with the typed `held`
      // unless the request carries `takeover`.
      if (message.type === "terminal_take_control") {
        const granted = state.control === "grant" || message.takeover === true;
        if (granted) state.generation += 1;
        send({
          type: "terminal_control_result",
          attachment_id: message.attachment_id,
          granted,
          reason: granted ? null : "held",
          lease_generation: state.generation,
        });
        return;
      }

      // Release is idempotent and always answers `released`.
      if (message.type === "terminal_release_control") {
        send({
          type: "terminal_control_result",
          attachment_id: message.attachment_id,
          granted: false,
          reason: "released",
          lease_generation: state.generation,
        });
        return;
      }

      if (
        message.type === "terminal_input" ||
        message.type === "terminal_paste"
      ) {
        send({
          type: "terminal_write_outcome",
          attachment_id: message.attachment_id,
          terminal_id: message.terminal_id,
          client_write_seq: message.client_write_seq,
          outcome: state.write,
          reason: state.write === "indeterminate" ? INDETERMINATE_REASON : null,
        });
        return;
      }
    });
  });

  return {
    sent: (type) => received.filter((frame) => frame.type === type),
    displace: () => {
      // The lease really moved, so the generation really moves with it —
      // otherwise every later frame looks stale to a client that correctly
      // drops reordered lease traffic.
      state.generation += 1;
      state.send?.({
        type: "terminal_lease_lost",
        attachment_id: STREAM_ID,
        holder: OTHER_HOLDER,
        lease_generation: state.generation,
        daemon_epoch: DAEMON_EPOCH,
        seq: 1,
      });
    },
    answerControlWith: (mode) => {
      state.control = mode;
    },
    answerWritesWith: (mode) => {
      state.write = mode;
    },
  };
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
  // The roster filter defaults to agent-managed terminals and this fixture is a
  // plain shell, so the list stays empty until "All" is selected. Take the same
  // route a user does rather than making the fixture claim to be agent-managed.
  await page
    .getByRole("radiogroup", { name: "Terminal sessions shown" })
    .getByRole("radio", { name: "All", exact: true })
    .click();
  await expect(
    page.getByRole("list", { name: "Terminal sessions" }),
  ).toBeVisible({ timeout: 15_000 });
}

function terminalView(page: Page): Locator {
  return page.getByTestId("terminal-view");
}

/** The renderer's own input surface — what focus and typing actually land on. */
function terminalInput(page: Page): Locator {
  return terminalView(page).locator("textarea");
}

function scrollContainer(page: Page): Locator {
  return terminalView(page).locator(".wterm");
}

async function openAttachedTerminal(page: Page): Promise<void> {
  await openTerminalTab(page);
  await expect(terminalView(page)).toContainText("live-line-3", {
    timeout: 20_000,
  });
}

/** Focus is the lease request; the grant is what makes the pane writable. */
async function focusTerminal(
  page: Page,
  socket: TerminalSocket,
): Promise<void> {
  await terminalInput(page).focus();
  await expect
    .poll(() => socket.sent("terminal_take_control").length, {
      timeout: 10_000,
    })
    .toBeGreaterThan(0);
}

/**
 * A real `ClipboardEvent` carrying a real `DataTransfer`, built in the page:
 * Playwright's `dispatchEvent` constructs a plain `Event` for `paste`, which
 * arrives with no `clipboardData` at all and so proves nothing.
 */
async function pasteInto(page: Page, text: string): Promise<void> {
  await terminalView(page).evaluate((element, value) => {
    const data = new DataTransfer();
    data.setData("text/plain", value);
    element.dispatchEvent(
      new ClipboardEvent("paste", {
        clipboardData: data,
        bubbles: true,
        cancelable: true,
      }),
    );
  }, text);
}

/**
 * Park the viewport in history, which is what puts the jump control on screen.
 * Retried rather than done once: a layout change re-fits the grid on a debounce
 * and that re-fit snaps the viewport back, exactly as a real scroll would be
 * undone — so the user's answer, scrolling again, is the honest one here.
 */
async function parkInHistory(page: Page): Promise<void> {
  await expect
    .poll(
      async () => {
        await scrollContainer(page).evaluate((element) => {
          element.scrollTop = 0;
        });
        return page.getByTestId("terminal-jump-to-bottom").count();
      },
      { timeout: 20_000 },
    )
    .toBeGreaterThan(0);
}

function boxShadow(control: Locator): Promise<string> {
  return control.evaluate((element) => getComputedStyle(element).boxShadow);
}

/**
 * Arrive at the control the way a keyboard user does, and prove the indicator
 * is visible there: the shared button ring is a box-shadow, so a real focus
 * indicator is a shadow that exists and differs from the resting one.
 */
async function tabToWithVisibleFocus(
  page: Page,
  control: Locator,
  resting: string,
): Promise<void> {
  await page.keyboard.press("Tab");
  await expect(control).toBeFocused();
  const focused = await boxShadow(control);
  expect(focused).not.toBe("none");
  expect(focused).not.toBe(resting);
}

/**
 * Dense controls keep their compact visual box: the 44×44 floor comes entirely
 * from the invisible `coarseHitAreaCls` ::before, never from inflating the row.
 */
async function assertInvisibleTouchFloor(control: Locator): Promise<void> {
  const measured = await control.evaluate((element) => {
    const before = getComputedStyle(element, "::before");
    return {
      expansion: {
        position: before.position,
        minWidth: before.minWidth,
        minHeight: before.minHeight,
      },
      visibleHeight: element.getBoundingClientRect().height,
    };
  });
  expect(measured.expansion).toEqual({
    position: "absolute",
    minWidth: "44px",
    minHeight: "44px",
  });
  expect(measured.visibleHeight).toBeLessThan(44);
}

/**
 * The floating jump control is not dense: under a coarse pointer its own box is
 * promoted to the floor, which is right for a free-standing round action.
 */
async function assertPromotedTouchFloor(control: Locator): Promise<void> {
  const box = await control.boundingBox();
  expect(box).not.toBeNull();
  expect(box?.width ?? 0).toBeGreaterThanOrEqual(44);
  expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
}

/**
 * A floating control has to actually float. Playwright calls an element that
 * has overflowed its container "visible" as long as it has a box, so a control
 * that lost its `absolute` — tailwind-merge will drop one against a later
 * `relative` — still passes every focus, click and size assertion while sitting
 * outside the pane entirely. Only comparing the two boxes catches that.
 */
async function assertFloatsInside(
  control: Locator,
  container: Locator,
): Promise<void> {
  const [box, bounds] = await Promise.all([
    control.boundingBox(),
    container.boundingBox(),
  ]);
  expect(box).not.toBeNull();
  expect(bounds).not.toBeNull();
  if (box === null || bounds === null) return;
  expect(box.x).toBeGreaterThanOrEqual(bounds.x);
  expect(box.y).toBeGreaterThanOrEqual(bounds.y);
  expect(box.x + box.width).toBeLessThanOrEqual(bounds.x + bounds.width);
  expect(box.y + box.height).toBeLessThanOrEqual(bounds.y + bounds.height);
}

const TIERS = [
  // 6.1.6 pins the 44×44 floor at this tier, and that floor exists only under a
  // coarse pointer, so this is the tier that emulates touch.
  { label: "440x956", width: 440, height: 956, coarse: true },
  { label: "932x430", width: 932, height: 430, coarse: false },
  { label: "1440x900", width: 1440, height: 900, coarse: false },
];

for (const tier of TIERS) {
  test.describe(`terminal control lease at ${tier.label}`, () => {
    test.use({
      viewport: { width: tier.width, height: tier.height },
      hasTouch: tier.coarse,
    });

    test("lease loss goes read-only and paste is bracketed", async ({
      page,
    }) => {
      await installApiMocks(page, "dark");
      const socket = await installTerminalSocket(page);
      await openAttachedTerminal(page);

      await focusTerminal(page, socket);
      expect(socket.sent("terminal_take_control")[0]).toMatchObject({
        terminal_id: TERMINAL_ID,
        attachment_id: STREAM_ID,
        takeover: false,
      });

      await pasteInto(page, PASTE_TEXT);
      await expect
        .poll(() => socket.sent("terminal_paste").length, { timeout: 10_000 })
        .toBe(1);
      expect(socket.sent("terminal_paste")[0]).toMatchObject({
        terminal_id: TERMINAL_ID,
        attachment_id: STREAM_ID,
        text: PASTE_TEXT,
      });
      // One frame, not three: the daemon brackets it, so the newlines inside
      // never reach the shell as separate submissions.
      expect(socket.sent("terminal_input")).toHaveLength(0);

      // Another session takes the lease out from under this one.
      socket.answerControlWith("refuse");
      socket.displace();

      const notice = page.getByTestId("terminal-read-only");
      await expect(notice).toBeVisible();
      await expect(notice).toContainText(
        "Read-only — another session has control",
      );

      // Typing while displaced asks for the lease again and is refused; nothing
      // reaches the terminal in the meantime.
      const controlRequests = socket.sent("terminal_take_control").length;
      await page.keyboard.type("x");
      await expect
        .poll(() => socket.sent("terminal_take_control").length, {
          timeout: 10_000,
        })
        .toBeGreaterThan(controlRequests);
      expect(socket.sent("terminal_input")).toHaveLength(0);
      await expect(page.getByTestId("terminal-write-status")).toContainText(
        "Control refused: held.",
      );

      // Take-back carries `takeover`: a plain take is refused while the other
      // session still holds the lease.
      socket.answerControlWith("grant");
      await page.getByTestId("terminal-take-control").click();
      await expect
        .poll(() => socket.sent("terminal_take_control").at(-1)?.takeover, {
          timeout: 10_000,
        })
        .toBe(true);
      await expect(notice).toBeHidden();

      await terminalInput(page).focus();
      await page.keyboard.type("y");
      await expect
        .poll(() => socket.sent("terminal_input").length, { timeout: 10_000 })
        .toBe(1);
      expect(socket.sent("terminal_input")[0]).toMatchObject({
        terminal_id: TERMINAL_ID,
        attachment_id: STREAM_ID,
        data: "y",
      });
    });

    test("lease controls are keyboard operable and sized for touch", async ({
      page,
    }) => {
      await installApiMocks(page, "dark");
      const socket = await installTerminalSocket(page);
      await openAttachedTerminal(page);
      await focusTerminal(page, socket);

      // An indeterminate outcome is the one settlement that offers retry and
      // discard, so it is what puts both controls on screen.
      socket.answerWritesWith("indeterminate");
      await page.keyboard.type("z");
      const status = page.getByTestId("terminal-write-status");
      await expect(status).toBeVisible({ timeout: 10_000 });

      await parkInHistory(page);

      // Lose the lease so the take-back control is on screen too.
      socket.answerControlWith("refuse");
      socket.displace();

      const notice = page.getByTestId("terminal-read-only");
      const takeBack = page.getByTestId("terminal-take-control");
      const jump = page.getByTestId("terminal-jump-to-bottom");
      const retry = status.getByRole("button", { name: "Retry", exact: true });
      const discard = status.getByRole("button", {
        name: "Discard",
        exact: true,
      });
      await expect(notice).toBeVisible();
      await expect(jump).toBeVisible({ timeout: 10_000 });
      await expect(retry).toBeVisible();
      await expect(discard).toBeVisible();

      // Non-colour state cues: a glyph and words carry both states, and the
      // icon-only jump control carries a name.
      await expect(notice.locator("svg")).toHaveCount(1);
      await expect(notice).toContainText("Read-only");
      await expect(status).toContainText(
        "Couldn’t confirm the last keystroke reached the terminal.",
      );
      await expect(status.locator("code")).toHaveText(INDETERMINATE_REASON);
      await expect(jump).toHaveAttribute(
        "aria-label",
        "Jump to newest terminal output",
      );

      const resting = {
        takeBack: await boxShadow(takeBack),
        jump: await boxShadow(jump),
        retry: await boxShadow(retry),
        discard: await boxShadow(discard),
      };

      // The pane's controls follow the terminal in the tab order, so a keyboard
      // user reaches every one of them by tabbing out of the renderer.
      await terminalInput(page).focus();
      await tabToWithVisibleFocus(page, takeBack, resting.takeBack);
      await tabToWithVisibleFocus(page, jump, resting.jump);
      await tabToWithVisibleFocus(page, retry, resting.retry);
      await tabToWithVisibleFocus(page, discard, resting.discard);

      // Both overlay controls float over the terminal at every tier; the touch
      // floor is only the coarse tier's extra obligation.
      await assertFloatsInside(takeBack, terminalView(page));
      await assertFloatsInside(jump, terminalView(page));

      if (tier.coarse) {
        await assertInvisibleTouchFloor(takeBack);
        await assertInvisibleTouchFloor(retry);
        await assertInvisibleTouchFloor(discard);
        await assertPromotedTouchFloor(jump);
      }

      // Operable, not merely focusable. The holder is still refusing plain
      // takes, so the one request that can win here is the takeover Enter
      // sends — and focusing the control must not spend the pane's one
      // in-flight control request on a doomed plain take first.
      const requests = socket.sent("terminal_take_control").length;
      await takeBack.focus();
      await page.keyboard.press("Enter");
      await expect
        .poll(() => socket.sent("terminal_take_control").length, {
          timeout: 10_000,
        })
        .toBe(requests + 1);
      expect(socket.sent("terminal_take_control").at(-1)).toMatchObject({
        takeover: true,
      });
      await expect(notice).toBeHidden();
    });
  });
}
