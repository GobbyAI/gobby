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
  seq_num?: number | null;
  source: string;
  model: string;
  title?: string | null;
}

interface SentChat {
  conversationId: string;
  dbSessionId: string;
  session: SessionSummary;
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

async function loadLiveCatalog(
  request: Parameters<typeof test>[0]["request"],
): Promise<Record<string, ProviderModelEntry>> {
  const authResponse = await request.get(getApiUrl("/api/auth/status"));
  expect(authResponse.ok()).toBeTruthy();
  const authStatus = await authResponse.json();
  test.skip(
    authStatus.auth_required && !authStatus.authenticated,
    "Live Gemini swap verification requires an authenticated daemon session.",
  );

  const providersResponse = await request.get(getApiUrl("/api/providers/models"));
  expect(providersResponse.ok()).toBeTruthy();
  const providersBody = await providersResponse.json();
  const providers = Array.isArray(providersBody?.providers)
    ? (providersBody.providers as ProviderModelEntry[])
    : [];

  return Object.fromEntries(providers.map((entry) => [entry.provider, entry]));
}

async function openLiveChat(
  page: Parameters<typeof test>[0]["page"],
  conversationId: string,
): Promise<void> {
  await page.goto(getLiveChatUrl());
  await page.evaluate((cid) => {
    localStorage.setItem("gobby-conversation-id", cid);
    localStorage.removeItem("gobby-db-session-id");
    localStorage.removeItem("gobby-selected-provider");
  }, conversationId);
  await page.reload();
  await expect(page.getByLabel("Select provider and model")).toBeVisible();
  await expect(page.getByRole("textbox", { name: /message input/i })).toBeVisible();
}

async function selectProviderModel(
  page: Parameters<typeof test>[0]["page"],
  modelLabel: string,
): Promise<void> {
  await page.getByLabel("Select provider and model").click();
  await expect(page.getByText(modelLabel)).toBeVisible();
  await page.getByText(modelLabel).click();
}

async function waitForSessionSummary(
  request: Parameters<typeof test>[0]["request"],
  dbSessionId: string,
  expectedModel: string,
): Promise<SessionSummary> {
  const deadline = Date.now() + PROMPT_TIMEOUT_MS;

  while (Date.now() < deadline) {
    const response = await request.get(getApiUrl(`/api/sessions/${dbSessionId}`));
    if (response.ok()) {
      const body = await response.json();
      const session = body?.session as SessionSummary | undefined;
      if (
        session?.id &&
        session.ref &&
        session.source === "gemini" &&
        session.model === expectedModel
      ) {
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
  token?: string,
): Promise<string> {
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

      if (token) {
        const found = assistantMessages.find((content: string) => content.includes(token));
        if (found) {
          return found;
        }
      } else if (assistantMessages.length > 0) {
        return assistantMessages[assistantMessages.length - 1];
      }
    }
    await pause(1000);
  }

  throw new Error(
    token
      ? `Timed out waiting for assistant token ${token} in ${dbSessionId}`
      : `Timed out waiting for an assistant response in ${dbSessionId}`,
  );
}

async function sendPromptAndWait(
  page: Parameters<typeof test>[0]["page"],
  request: Parameters<typeof test>[0]["request"],
  prompt: string,
  token: string | null,
  expectedModel: string,
): Promise<SentChat> {
  const initialMessageCount = await page.locator(".message-content").count();
  const input = page.getByRole("textbox", { name: /message input/i });
  await input.fill(prompt);
  await input.press("Enter");

  await page.waitForFunction(
    () => Boolean(localStorage.getItem("gobby-db-session-id")),
    null,
    { timeout: 15_000 },
  );

  const dbSessionId = await page.evaluate(() => localStorage.getItem("gobby-db-session-id"));
  const conversationId = await page.evaluate(() => localStorage.getItem("gobby-conversation-id"));
  expect(dbSessionId).toBeTruthy();
  expect(conversationId).toBeTruthy();

  const session = await waitForSessionSummary(request, dbSessionId!, expectedModel);
  await waitForAssistantToken(request, dbSessionId!, token || undefined);
  await expect
    .poll(async () => page.locator(".message-content").count(), {
      timeout: PROMPT_TIMEOUT_MS,
    })
    .toBeGreaterThan(initialMessageCount + 1);
  await expect(page.getByText("Thinking...")).toHaveCount(0, { timeout: PROMPT_TIMEOUT_MS });
  await expect(page.getByText("Generation failed")).toHaveCount(0);

  return {
    conversationId: conversationId!,
    dbSessionId: dbSessionId!,
    session,
  };
}

async function startNewChat(
  page: Parameters<typeof test>[0]["page"],
  previousConversationId: string,
): Promise<void> {
  await page.locator('button[title="New Chat"]').click();
  await page.waitForFunction(
    (prevConversationId) =>
      localStorage.getItem("gobby-conversation-id") !== prevConversationId &&
      !localStorage.getItem("gobby-db-session-id"),
    previousConversationId,
    { timeout: 15_000 },
  );
  await expect(page.locator(".command-bar-session")).toContainText("New Chat Session");
}

async function swapToSession(
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

test.describe("Live Gemini web chat swap verification", () => {
  test.skip(
    !process.env[LIVE_E2E_FLAG],
    `Set ${LIVE_E2E_FLAG}=1 to run real daemon-backed Gemini swap verification.`,
  );

  test("can start Gemini chats, swap back to an earlier chat, and still get responses", async ({
    page,
    request,
  }) => {
    test.setTimeout(12 * 60 * 1000);

    const catalog = await loadLiveCatalog(request);
    const gemini = catalog.gemini;
    expect(gemini, "Gemini must exist in /api/providers/models").toBeTruthy();
    expect(gemini.available, "Gemini must be available").toBeTruthy();
    expect(gemini.models.length, "Gemini must expose at least one model").toBeGreaterThan(0);

    const model = gemini.models[0];
    const runId = Date.now().toString(36);

    await openLiveChat(page, `live-gemini-swap-${runId}`);
    await selectProviderModel(page, model.label);

    const firstChat = await sendPromptAndWait(
      page,
      request,
      "Hi Gobby",
      null,
      model.value,
    );
    await expect(page.locator(".command-bar-session")).toContainText(firstChat.session.ref);

    await startNewChat(page, firstChat.conversationId);
    await selectProviderModel(page, model.label);

    const secondChat = await sendPromptAndWait(
      page,
      request,
      "Hi Gobby",
      null,
      model.value,
    );
    expect(secondChat.conversationId).not.toBe(firstChat.conversationId);
    expect(secondChat.dbSessionId).not.toBe(firstChat.dbSessionId);

    await swapToSession(page, firstChat.session.ref);
    await expect(page.locator(".command-bar-session")).toContainText(firstChat.session.ref);
    await expect(page.getByText("Generation failed")).toHaveCount(0);

    const followupToken = `live-gemini-followup-${runId}`;
    const followup = await sendPromptAndWait(
      page,
      request,
      `Respond with one short sentence that ends with ${followupToken}.`,
      followupToken,
      model.value,
    );
    expect(followup.dbSessionId).toBe(firstChat.dbSessionId);
  });
});
