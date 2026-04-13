import { expect, test } from "@playwright/test";

const LIVE_E2E_FLAG = "GOBBY_LIVE_PROVIDER_E2E";
const LIVE_E2E_URL = "GOBBY_LIVE_PROVIDER_E2E_URL";
const DEFAULT_CHAT_ROUTE = "/#chat";
const PROMPT_TIMEOUT_MS = 180_000;

interface ProviderModelEntry {
  provider: string;
  available: boolean;
  models: Array<{ value: string; label: string; hidden?: boolean }>;
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

async function loadCodexModels(
  request: Parameters<typeof test>[0]["request"],
): Promise<Array<{ value: string; label: string }>> {
  const authResponse = await request.get(getApiUrl("/api/auth/status"));
  expect(authResponse.ok()).toBeTruthy();
  const authStatus = await authResponse.json();
  test.skip(
    authStatus.auth_required && !authStatus.authenticated,
    "Live Codex verification requires an authenticated daemon session.",
  );

  const providersResponse = await request.get(getApiUrl("/api/providers/models"));
  expect(providersResponse.ok()).toBeTruthy();
  const providersBody = await providersResponse.json();
  const providers = Array.isArray(providersBody?.providers)
    ? (providersBody.providers as ProviderModelEntry[])
    : [];
  const codex = providers.find((entry) => entry.provider === "codex");

  expect(codex, "Codex must exist in /api/providers/models").toBeTruthy();
  expect(codex?.available, "Codex must be available").toBeTruthy();

  const visibleModels = (codex?.models || []).filter((model) => !model.hidden);
  expect(visibleModels.length, "Codex must expose at least two visible models").toBeGreaterThan(1);

  return visibleModels.slice(0, 2).map((model) => ({
    value: model.value,
    label: model.label,
  }));
}

async function openFreshChat(
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
  const option = page.getByRole("button", { name: modelLabel, exact: true });
  await expect(option).toBeVisible();
  await option.click();
  await expect(page.getByRole("textbox", { name: /message input/i })).toBeVisible();
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
        session.source === "codex" &&
        session.model === expectedModel
      ) {
        return session;
      }
    }
    await pause(1000);
  }

  throw new Error(`Timed out waiting for Codex session metadata for ${dbSessionId}`);
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

      const failed = assistantMessages.find(
        (content: string) =>
          content.includes("Error:") ||
          content.includes("Generation failed") ||
          content.includes("missing field turnId"),
      );
      if (failed) {
        throw new Error(`Codex returned an error instead of a response: ${failed}`);
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
  expectedModel: string,
): Promise<{ dbSessionId: string; session: SessionSummary }> {
  const initialMessageCount = await page.getByTestId("chat-message-content").count();
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

  const session = await waitForSessionSummary(request, dbSessionId!, expectedModel);
  await waitForAssistantToken(request, dbSessionId!, token);
  await expect
    .poll(async () => page.getByTestId("chat-message-content").count(), {
      timeout: PROMPT_TIMEOUT_MS,
    })
    .toBeGreaterThan(initialMessageCount + 1);

  await expect(page.getByText("Thinking...")).toHaveCount(0, { timeout: PROMPT_TIMEOUT_MS });
  await expect(page.getByText("missing field turnId")).toHaveCount(0);
  await expect(page.getByText("Generation failed")).toHaveCount(0);

  return {
    dbSessionId: dbSessionId!,
    session,
  };
}

test.describe("Live Codex model switch verification", () => {
  test.skip(
    !process.env[LIVE_E2E_FLAG],
    `Set ${LIVE_E2E_FLAG}=1 to run real daemon-backed Codex verification.`,
  );

  test("can switch Codex models on the same web-chat session and continue responding", async ({
    page,
    request,
  }) => {
    test.setTimeout(8 * 60 * 1000);

    const [firstModel, secondModel] = await loadCodexModels(request);
    const runId = Date.now().toString(36);

    await openFreshChat(page, `live-codex-switch-${runId}`);
    await selectProviderModel(page, firstModel.label);

    const firstToken = `live-codex-first-${runId}`;
    const firstTurn = await sendPromptAndWait(
      page,
      request,
      `Reply with exactly ${firstToken}.`,
      firstToken,
      firstModel.value,
    );

    await selectProviderModel(page, secondModel.label);

    const secondToken = `live-codex-second-${runId}`;
    const secondTurn = await sendPromptAndWait(
      page,
      request,
      `Reply with exactly ${secondToken}.`,
      secondToken,
      secondModel.value,
    );

    expect(secondTurn.dbSessionId).toBe(firstTurn.dbSessionId);
    await expect(page.getByTestId("chat-session-selector")).toContainText(firstTurn.session.ref);
  });
});
