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

async function waitForAssistantResponse(
  request: Parameters<typeof test>[0]["request"],
  dbSessionId: string,
): Promise<void> {
  const deadline = Date.now() + PROMPT_TIMEOUT_MS;

  while (Date.now() < deadline) {
    const response = await request.get(
      getApiUrl(`/api/sessions/${dbSessionId}/messages?limit=50&offset=0`),
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

      if (assistantMessages.length > 0) {
        return;
      }
    }
    await pause(1000);
  }

  throw new Error(`Timed out waiting for a Gemini assistant response in ${dbSessionId}`);
}

test.describe("Live Gemini Hi Gobby verification", () => {
  test.skip(
    !process.env[LIVE_E2E_FLAG],
    `Set ${LIVE_E2E_FLAG}=1 to run real daemon-backed Gemini verification.`,
  );

  test("fresh Gemini chat responds to Hi Gobby", async ({ page, request }) => {
    test.setTimeout(6 * 60 * 1000);

    const model = await loadGeminiModel(request);
    const conversationId = `live-gemini-hi-${Date.now().toString(36)}`;

    await page.goto(getLiveChatUrl());
    await page.evaluate((cid) => {
      localStorage.setItem("gobby-conversation-id", cid);
      localStorage.removeItem("gobby-db-session-id");
      localStorage.removeItem("gobby-selected-provider");
    }, conversationId);
    await page.reload();

    await expect(page.getByLabel("Select provider and model")).toBeVisible();
    await page.getByLabel("Select provider and model").click();
    await expect(page.getByText(model.label)).toBeVisible();
    await page.getByText(model.label).click();

    const initialMessageCount = await page.locator(".message-content").count();
    const input = page.getByRole("textbox", { name: /message input/i });
    await input.fill("Hi Gobby");
    await input.press("Enter");

    await page.waitForFunction(
      () => Boolean(localStorage.getItem("gobby-db-session-id")),
      null,
      { timeout: 15_000 },
    );

    const dbSessionId = await page.evaluate(() => localStorage.getItem("gobby-db-session-id"));
    expect(dbSessionId).toBeTruthy();

    await waitForAssistantResponse(request, dbSessionId!);
    await expect
      .poll(async () => page.locator(".message-content").count(), {
        timeout: PROMPT_TIMEOUT_MS,
      })
      .toBeGreaterThan(initialMessageCount + 1);
    await expect(page.getByText("Thinking...")).toHaveCount(0, { timeout: PROMPT_TIMEOUT_MS });
    await expect(page.getByText("Generation failed")).toHaveCount(0);
  });
});
