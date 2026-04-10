import { expect, test } from "@playwright/test";

const LIVE_E2E_FLAG = "GOBBY_LIVE_PROVIDER_E2E";
const LIVE_E2E_URL = "GOBBY_LIVE_PROVIDER_E2E_URL";
const DEFAULT_CHAT_ROUTE = "/#chat";
const PROMPT_TIMEOUT_MS = 120_000;

interface ProviderModelEntry {
  provider: string;
  available: boolean;
  models: Array<{ value: string; label: string }>;
  source: string;
}

function sanitizeToken(value: string): string {
  return value.replace(/[^a-z0-9]+/gi, "-").replace(/^-+|-+$/g, "").toLowerCase();
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

async function loadLiveCatalog(
  request: Parameters<typeof test>[0]["request"],
): Promise<Record<string, ProviderModelEntry>> {
  const authResponse = await request.get(getApiUrl("/api/auth/status"));
  expect(authResponse.ok()).toBeTruthy();
  const authStatus = await authResponse.json();
  test.skip(
    authStatus.auth_required && !authStatus.authenticated,
    "Live provider verification requires an authenticated daemon session.",
  );

  const providersResponse = await request.get(getApiUrl("/api/providers/models"));
  expect(providersResponse.ok()).toBeTruthy();
  const providersBody = await providersResponse.json();
  const providers = Array.isArray(providersBody?.providers)
    ? (providersBody.providers as ProviderModelEntry[])
    : [];

  return Object.fromEntries(providers.map((entry) => [entry.provider, entry]));
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
  await expect(page.getByRole("textbox", { name: /message input/i })).toBeVisible();
}

async function selectProviderModel(
  page: Parameters<typeof test>[0]["page"],
  modelLabel: string,
): Promise<void> {
  await page.getByLabel("Select provider and model").click();
  await expect(page.getByText(modelLabel)).toBeVisible();
  await page.getByText(modelLabel).click();
  await expect(page.getByRole("textbox", { name: /message input/i })).toBeVisible();
}

async function sendProbePrompt(
  page: Parameters<typeof test>[0]["page"],
  request: Parameters<typeof test>[0]["request"],
  provider: string,
  model: { value: string; label: string },
): Promise<void> {
  const token = `live-${sanitizeToken(provider)}-${sanitizeToken(model.value)}`;
  const prompt = `Reply with exactly ${token} and nothing else.`;

  const input = page.getByRole("textbox", { name: /message input/i });
  await input.fill(prompt);
  await input.press("Enter");

  await page.waitForFunction(
    () => localStorage.getItem("gobby-db-session-id"),
    null,
    { timeout: 15_000 },
  );

  const dbSessionId = await page.evaluate(() => localStorage.getItem("gobby-db-session-id"));
  expect(dbSessionId).toBeTruthy();

  await expect
    .poll(
      async () => {
        const sessionResponse = await request.get(getApiUrl(`/api/sessions/${dbSessionId}`));
        if (!sessionResponse.ok()) {
          return null;
        }
        const sessionBody = await sessionResponse.json();
        const session = sessionBody.session;
        if (!session) {
          return null;
        }
        return `${session.source}:${session.model}`;
      },
      { timeout: PROMPT_TIMEOUT_MS },
    )
    .toBe(`${provider}:${model.value}`);

  await expect
    .poll(
      async () => {
        const messagesResponse = await request.get(
          getApiUrl(`/api/sessions/${dbSessionId}/messages?limit=100&offset=0`),
        );
        if (!messagesResponse.ok()) {
          return false;
        }
        const messagesBody = await messagesResponse.json();
        const messages = Array.isArray(messagesBody.messages) ? messagesBody.messages : [];
        return messages.some(
          (message: { role?: string; content?: string | null }) =>
            message.role === "assistant" && (message.content || "").includes(token),
        );
      },
      { timeout: PROMPT_TIMEOUT_MS },
    )
    .toBe(true);
}

test.describe("Live provider picker verification", () => {
  test.skip(
    !process.env[LIVE_E2E_FLAG],
    `Set ${LIVE_E2E_FLAG}=1 to run real daemon-backed provider verification.`,
  );

  test("Gemini and Codex models can each send and receive a live web chat response", async ({
    page,
    request,
  }) => {
    test.setTimeout(10 * 60 * 1000);

    const catalog = await loadLiveCatalog(request);
    const providersToVerify = ["gemini", "codex"] as const;

    for (const providerName of providersToVerify) {
      const provider = catalog[providerName];
      expect(provider, `Provider ${providerName} must exist in /api/providers/models`).toBeTruthy();
      expect(provider.available, `Provider ${providerName} must be available`).toBeTruthy();
      expect(provider.models.length, `Provider ${providerName} must expose models`).toBeGreaterThan(
        0,
      );

      for (const model of provider.models) {
        await openFreshChat(page, `live-${providerName}-${sanitizeToken(model.value)}`);
        await selectProviderModel(page, model.label);
        await sendProbePrompt(page, request, providerName, model);
      }
    }
  });
});
