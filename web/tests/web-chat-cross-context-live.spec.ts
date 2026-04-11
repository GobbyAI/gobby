import { expect, test } from "@playwright/test";

const LIVE_E2E_FLAG = "GOBBY_LIVE_PROVIDER_E2E";
const LIVE_E2E_URL = "GOBBY_LIVE_PROVIDER_E2E_URL";
const DEFAULT_CHAT_ROUTE = "/#chat";
const PROMPT_TIMEOUT_MS = 180_000;

interface ProviderModelEntry {
  provider: string;
  available: boolean;
  models: Array<{ value: string; label: string }>;
  source: string;
}

interface SessionSummary {
  id: string;
  ref: string;
  source: string;
  model: string;
}

function getLiveChatUrl(): string {
  return process.env[LIVE_E2E_URL] || DEFAULT_CHAT_ROUTE;
}

function getApiUrl(path: string): string {
  const liveUrl = process.env[LIVE_E2E_URL];
  if (!liveUrl) {
    return path;
  }
  const origin = new URL(liveUrl).origin;
  return new URL(path, origin).toString();
}

function pause(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function loadGeminiModel(
  request: Parameters<typeof test>[0]["request"],
): Promise<{ value: string; label: string }> {
  const authResponse = await request.get(getApiUrl("/api/auth/status"));
  expect(authResponse.ok()).toBeTruthy();
  const authStatus = await authResponse.json();
  test.skip(
    authStatus.auth_required && !authStatus.authenticated,
    "Live Gemini verification requires an authenticated daemon session.",
  );

  const providersResponse = await request.get(getApiUrl("/api/providers/models"));
  expect(providersResponse.ok()).toBeTruthy();
  const providersBody = await providersResponse.json();
  const providers = Array.isArray(providersBody?.providers)
    ? (providersBody.providers as ProviderModelEntry[])
    : [];
  const gemini = providers.find((entry) => entry.provider === "gemini");

  expect(gemini, "Gemini must exist in /api/providers/models").toBeTruthy();
  expect(gemini?.available, "Gemini must be available").toBeTruthy();
  expect(gemini?.models.length, "Gemini must expose at least one model").toBeGreaterThan(0);

  return gemini!.models[0];
}

async function openFreshChat(
  page: Parameters<typeof test>[0]["page"],
  conversationId: string,
): Promise<void> {
  await page.goto(getLiveChatUrl());
  await page.evaluate((cid) => {
    localStorage.setItem("gobby-conversation-id", cid);
    localStorage.removeItem("gobby-db-session-id");
    localStorage.setItem("gobby-selected-provider", "gemini");
  }, conversationId);
  await page.reload();
  await expect(page.getByLabel("Select provider and model")).toBeVisible();
  await expect(page.getByRole("textbox", { name: /message input/i })).toBeVisible();
}

async function configureGeminiModel(
  request: Parameters<typeof test>[0]["request"],
  modelValue: string,
): Promise<void> {
  const response = await request.put(getApiUrl("/api/config/ui-settings"), {
    data: { model: modelValue },
  });
  expect(response.ok()).toBeTruthy();
}

async function waitForSessionSummary(
  request: Parameters<typeof test>[0]["request"],
  dbSessionId: string,
): Promise<SessionSummary> {
  const deadline = Date.now() + PROMPT_TIMEOUT_MS;

  while (Date.now() < deadline) {
    const response = await request.get(getApiUrl(`/api/sessions/${dbSessionId}`));
    if (response.ok()) {
      const body = await response.json();
      const session = body?.session as SessionSummary | undefined;
      if (session?.id && session.ref && session.source === "gemini") {
        return session;
      }
    }
    await pause(1000);
  }

  throw new Error(`Timed out waiting for Gemini session metadata for ${dbSessionId}`);
}

async function waitForAssistantToken(
  request: Parameters<typeof test>[0]["request"],
  dbSessionId: string,
  token: string,
): Promise<void> {
  const deadline = Date.now() + PROMPT_TIMEOUT_MS;

  while (Date.now() < deadline) {
    const response = await request.get(
      getApiUrl(`/api/sessions/${dbSessionId}/messages?limit=200&offset=0`),
    );
    if (response.ok()) {
      const body = await response.json();
      const messages = Array.isArray(body?.messages) ? body.messages : [];
      const assistantMessages = messages
        .filter((message: { role?: string; content?: string | null }) => message.role === "assistant")
        .map((message: { content?: string | null }) => (message.content || "").trim())
        .filter(Boolean);

      const failed = assistantMessages.find((content: string) =>
        content.includes("Generation failed"),
      );
      if (failed) {
        throw new Error(`Gemini returned an error instead of a response: ${failed}`);
      }

      if (assistantMessages.some((content: string) => content.includes(token))) {
        return;
      }
    }
    await pause(1000);
  }

  throw new Error(`Timed out waiting for assistant token ${token} in ${dbSessionId}`);
}

async function sendPromptAndWait(
  page: Parameters<typeof test>[0]["page"],
  request: Parameters<typeof test>[0]["request"],
  prompt: string,
  token: string,
): Promise<{ dbSessionId: string; session: SessionSummary }> {
  const input = page.getByRole("textbox", { name: /message input/i });
  await input.fill(prompt);
  await input.press("Enter");

  await page.waitForFunction(
    () => Boolean(localStorage.getItem("gobby-db-session-id")),
    null,
    { timeout: 15_000 },
  );

  const dbSessionId = await page.evaluate(() => localStorage.getItem("gobby-db-session-id"));
  expect(dbSessionId).toBeTruthy();

  const session = await waitForSessionSummary(request, dbSessionId!);
  const assistantContent = await waitForAssistantToken(request, dbSessionId!, token);
  await expect(
    page
      .locator(".message-content")
      .filter({ hasText: token || assistantContent.slice(0, 120) })
      .last(),
  ).toBeVisible({
    timeout: PROMPT_TIMEOUT_MS,
  });

  return {
    dbSessionId: dbSessionId!,
    session,
  };
}

async function openSessionFromCommandPalette(
  page: Parameters<typeof test>[0]["page"],
  sessionRef: string,
): Promise<void> {
  await page.locator(".command-bar-session").click();
  const palette = page.getByRole("dialog", { name: "Command palette" });
  await expect(palette).toBeVisible();
  const search = palette.locator("input");
  await search.fill(sessionRef);
  const option = palette.getByRole("option").filter({ hasText: sessionRef }).first();
  await expect(option).toBeVisible();
  await option.click();
  await expect(palette).toBeHidden();
}

test.describe("Live Gemini cross-context continuity verification", () => {
  test.skip(
    !process.env[LIVE_E2E_FLAG],
    `Set ${LIVE_E2E_FLAG}=1 to run real daemon-backed Gemini verification.`,
  );

  test("can reopen the same Gemini web chat in a second browser context and continue it", async ({
    browser,
    page,
    request,
  }) => {
    test.setTimeout(10 * 60 * 1000);

    const model = await loadGeminiModel(request);
    const runId = Date.now().toString(36);

    await configureGeminiModel(request, model.value);
    await openFreshChat(page, `live-gemini-cross-context-${runId}`);

    const firstToken = `live-gemini-context-one-${runId}`;
    const firstTurn = await sendPromptAndWait(
      page,
      request,
      `Reply with exactly ${firstToken}.`,
      firstToken,
    );

    const secondContext = await browser.newContext();
    const secondPage = await secondContext.newPage();

    await secondPage.goto(getLiveChatUrl());
    await secondPage.evaluate(() => {
      localStorage.removeItem("gobby-conversation-id");
      localStorage.removeItem("gobby-db-session-id");
      localStorage.removeItem("gobby-selected-provider");
    });
    await secondPage.reload();
    await expect(secondPage.locator(".command-bar-session")).toContainText("New Chat Session");

    await openSessionFromCommandPalette(secondPage, firstTurn.session.ref);

    await secondPage.waitForFunction(
      (expectedId) => localStorage.getItem("gobby-db-session-id") === expectedId,
      firstTurn.dbSessionId,
      { timeout: 15_000 },
    );

    await expect(secondPage.locator(".command-bar-session")).toContainText(firstTurn.session.ref);
    await expect(
      secondPage
        .locator(".message-content")
        .filter({ hasText: firstToken })
        .last(),
    ).toBeVisible({
      timeout: PROMPT_TIMEOUT_MS,
    });

    const secondToken = `live-gemini-context-two-${runId}`;
    const secondTurn = await sendPromptAndWait(
      secondPage,
      request,
      `Reply with exactly ${secondToken}.`,
      secondToken,
    );

    expect(secondTurn.dbSessionId).toBe(firstTurn.dbSessionId);
    await waitForAssistantToken(request, firstTurn.dbSessionId, secondToken);
    await secondContext.close();
  });
});
