// Pinned measurement behind ATTACH_HISTORY_LINES (src/gobby/agents/tmux/history.py).
// The bound is a renderer budget, so it can only be justified by rendering: this
// times a full window from the history frame landing to a settled scrollback, at
// a 4x CPU throttle, one warm-up plus five samples, median, on both the Ghostty
// wasm core and the built-in wterm fallback. It also reports which line numbers
// survived, because a core whose ring evicts the front delivers less than it was
// sent no matter how fast it renders.
//
//   PERF_RUN=1 npx playwright test tests/history-perf.spec.ts --project=chromium
//
// Opt-in: a single core costs ~20s per line count, which an ordinary suite run
// should not pay. Sweep with PERF_LINES.
import { expect, test, type Page } from "@playwright/test";

const PERF_RUN = Boolean(process.env.PERF_RUN);
const HISTORY_LINES = Number(process.env.PERF_LINES ?? 2000);
const SAMPLES = Number(process.env.PERF_SAMPLES ?? 5);
const WARMUP = Number(process.env.PERF_WARMUP ?? 1);
const ESC = "";
const PAD = " lorem ipsum dolor sit amet consectetur".repeat(
  Number(process.env.PERF_PAD ?? 1),
);

const SESSION = {
  name: "perf-session",
  socket: "default",
  pane_pid: 1,
  pane_dead: false,
  pane_title: "Perf",
  pane_command: null,
  pane_path: null,
  window_name: "perf",
  session_title: "Perf",
  gobby_session_id: null,
  agent_managed: false,
  agent_run_id: null,
  attached_bridge: null,
};

function history(): string {
  return (
    Array.from(
      { length: HISTORY_LINES },
      (_, i) => `${ESC}[3${i % 8}mhistory-line-${i + 1}${PAD}${ESC}[0m`,
    ).join("\r\n") + `${ESC}[0m`
  );
}

async function mocks(page: Page, box: { sendEpoch: number }) {
  await page.addInitScript(() => {
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
    const marks = window as unknown as Record<string, number>;
    marks.__lastRowMutation = 0;
    const observer = new MutationObserver((records) => {
      // Only scrollback-row churn counts. The renderer keeps touching the
      // cursor element forever, which would never let the page go quiet.
      for (const record of records) {
        for (const node of Array.from(record.addedNodes)) {
          if (!(node instanceof Element)) continue;
          const hit =
            node.classList.contains("term-scrollback-row") ||
            node.querySelector(".term-scrollback-row") !== null;
          if (hit) {
            marks.__lastRowMutation = performance.now();
            marks.__rowAdds = (marks.__rowAdds ?? 0) + 1;
            return;
          }
        }
      }
    });
    observer.observe(document, { childList: true, subtree: true });
  });
  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    async (route) => {
      const path = new URL(route.request().url()).pathname;
      const json = (b: unknown) =>
        route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(b),
        });
      if (path === "/api/auth/status") return json({ authenticated: true });
      if (path === "/api/config/ui-settings")
        return json({
          selectedProjectId: "p1",
          model: "opus",
          theme: "dark",
          defaultChatMode: "plan",
          fontSize: 16,
        });
      if (path === "/api/providers")
        return json({ providers: [{ name: "claude", available: true }] });
      if (path === "/api/providers/models") return json({ providers: [] });
      if (path === "/api/voice/status")
        return json({ enabled: false, stt_available: false });
      if (path === "/api/projects" || path === "/api/files/projects")
        return json([
          {
            id: "p1",
            name: "p",
            display_name: "P",
            repo_path: "/tmp/p",
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
      if (path === "/api/agents/running") return json({ agents: [] });
      if (path === "/api/sessions") return json({ sessions: [], total: 0 });
      if (path === "/api/tasks")
        return json({ tasks: [], total: 0, stats: {}, limit: 200, offset: 0 });
      return json({});
    },
  );
  await page.routeWebSocket("**/ws", (ws) => {
    let activated = false;
    ws.onMessage((raw) => {
      const m = JSON.parse(String(raw)) as Record<string, unknown>;
      if (m.type === "subscribe") {
        ws.send(
          JSON.stringify({
            type: "connection_established",
            conversation_ids: [],
          }),
        );
        ws.send(
          JSON.stringify({ type: "subscribe_success", events: m.events ?? [] }),
        );
      }
      if (m.type === "tmux_list_sessions")
        ws.send(
          JSON.stringify({
            type: "tmux_sessions_list",
            live_cli_session_ids: [],
            sessions: [SESSION],
          }),
        );
      if (m.type === "tmux_attach")
        ws.send(
          JSON.stringify({
            type: "tmux_attach_result",
            request_id: m.request_id,
            success: true,
            streaming_id: "perf-stream",
            session_name: m.session_name,
            socket: m.socket,
          }),
        );
      if (m.type === "tmux_resize" && !activated) {
        activated = true;
        // performance.timeOrigin and Date.now() are the same epoch clock on
        // this machine, so the two sides are directly comparable.
        box.sendEpoch = Date.now();
        ws.send(
          JSON.stringify({
            type: "terminal_attach_history",
            streaming_id: String(m.streaming_id),
            text: history(),
            truncated: true,
            unavailable: false,
            dropped_bytes: 0,
            total_bytes: 0,
          }),
        );
      }
    });
  });
}

async function measure(
  page: Page,
  box: { sendEpoch: number },
): Promise<{
  settledEpoch: number;
  rows: number;
  nodes: number;
  first: string;
  cols: number;
  firstNum: number;
  lastScrollbackNum: number;
  gridRows: number;
  lastGridNum: number;
}> {
  await page.goto("/");
  await page.getByRole("button", { name: "Show activity panel" }).click();
  const trigger = page.locator(".activity-panel-mobile-trigger");
  await expect(trigger).toContainText("Sessions");
  await trigger.click();
  await page
    .locator(".activity-panel-mobile-menu")
    .getByRole("button", { name: "Terminal", exact: true })
    .click();
  await expect(
    page.getByRole("list", { name: "Terminal sessions" }),
  ).toBeVisible({ timeout: 60_000 });

  await page
    .locator('[data-testid="terminal-view"] .wterm')
    .waitFor({ timeout: 120_000 });

  return page.evaluate(async () => {
    const find = () =>
      document.querySelector<HTMLElement>(
        '[data-testid="terminal-view"] .wterm',
      );
    // Every wait here is driven by animation frames. write() renders through
    // rAF, so frames are the clock the renderer actually runs on, and a wall
    // timer would only add a second clock to reason about. What is reported is
    // the last row mutation itself, so how many frames detection takes never
    // reaches the measurement.
    const frame = () =>
      new Promise<number>((resolve) => requestAnimationFrame(resolve));
    // The view remounts when streamingId changes, so the element the outer
    // waitFor saw can be detached by the time this evaluate starts.
    let wterm = find();
    for (let waited = 0; waited < 600 && !wterm; waited += 1) {
      await frame();
      wterm = find();
    }
    if (!wterm) throw new Error("no terminal");
    const marks = window as unknown as Record<string, number>;
    const term = wterm;
    const count = () => term.querySelectorAll(".term-scrollback-row").length;
    // Settled is the plan's definition: the expected rows exist and survive
    // unchanged across subsequent frames.
    const QUIET_FRAMES = 45;
    let quiet = 0;
    let last = count();
    for (let frames = 0; quiet < QUIET_FRAMES; frames += 1) {
      if (frames > 3600) throw new Error(`never settled at ${count()}`);
      await frame();
      const now = count();
      // The floor keeps a buffer that has not started rendering from reading
      // as one that finished.
      if (now !== last || now <= 100) quiet = 0;
      else quiet += 1;
      last = now;
    }
    const rowsEls = term.querySelectorAll(".term-scrollback-row");
    (window as unknown as Record<string, string>).__firstRow =
      rowsEls[0]?.textContent?.trim().slice(0, 32) ?? "";
    // Which lines survived is the question the row count alone cannot answer:
    // a short count with a high first line means the ring evicted the front,
    // while a short count starting at line 1 means the tail never arrived.
    const numberOf = (el: Element | undefined) =>
      Number(/history-line-(\d+)/.exec(el?.textContent ?? "")?.[1] ?? -1);
    const grid = term.querySelectorAll(".term-row");
    return {
      settledEpoch: performance.timeOrigin + marks.__lastRowMutation,
      rows: count(),
      nodes: term.querySelectorAll("*").length,
      first: (window as unknown as Record<string, string>).__firstRow,
      cols: term.querySelector(".term-row")?.childElementCount ?? -1,
      firstNum: numberOf(rowsEls[0]),
      lastScrollbackNum: numberOf(rowsEls[rowsEls.length - 1]),
      gridRows: grid.length,
      lastGridNum: Math.max(...Array.from(grid, (el) => numberOf(el)), -1),
    };
  });
}

for (const core of ["ghostty", "fallback"] as const) {
  test(`history render cost (${core})`, async ({ page }) => {
    test.skip(!PERF_RUN, "measurement run; set PERF_RUN=1");
    test.setTimeout(900_000);
    const box = { sendEpoch: 0 };
    const vw = Number(process.env.PERF_VW ?? 0);
    const vh = Number(process.env.PERF_VH ?? 0);
    if (vw && vh) await page.setViewportSize({ width: vw, height: vh });
    const client = await page.context().newCDPSession(page);
    await client.send("Emulation.setCPUThrottlingRate", {
      rate: Number(process.env.PERF_CPU ?? 4),
    });
    if (core === "fallback")
      await page.route("**/wasm/ghostty-vt.wasm", (route) =>
        route.fulfill({ status: 404, body: "" }),
      );
    await mocks(page, box);

    const samples: Array<{
      ms: number;
      rows: number;
      nodes: number;
      first: string;
      cols: number;
      firstNum: number;
      lastScrollbackNum: number;
      gridRows: number;
      lastGridNum: number;
    }> = [];
    for (let run = 0; run < WARMUP + SAMPLES; run += 1) {
      box.sendEpoch = 0;
      const result = await measure(page, box);
      if (!box.sendEpoch) throw new Error("history frame was never sent");
      if (run >= WARMUP)
        samples.push({ ...result, ms: result.settledEpoch - box.sendEpoch });
    }
    const times = samples.map((s) => s.ms).sort((a, b) => a - b);
    const median = times[Math.floor(times.length / 2)];
    console.log(
      `RESULT core=${core} lines=${HISTORY_LINES} median_ms=${median?.toFixed(1)} all=${times
        .map((t) => t.toFixed(0))
        .join(
          ",",
        )} rows=${samples[0]?.rows} nodes=${samples[0]?.nodes} vw=${vw || "default"} cols=${samples[0]?.cols}`,
    );
    console.log(
      `COVERAGE core=${core} sent=${HISTORY_LINES} scrollback=${samples[0]?.rows}` +
        ` grid=${samples[0]?.gridRows} firstLine=${samples[0]?.firstNum}` +
        ` lastScrollbackLine=${samples[0]?.lastScrollbackNum}` +
        ` lastGridLine=${samples[0]?.lastGridNum}`,
    );
    expect(median).toBeGreaterThan(0);
  });
}
