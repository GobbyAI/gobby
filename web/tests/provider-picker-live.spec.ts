import {
  expect,
  test,
  type APIRequestContext,
  type Page,
  type TestInfo,
} from "@playwright/test";

import { getProviderDisplayName } from "../src/lib/providerModels";

const LIVE_E2E_FLAG = "GOBBY_LIVE_PROVIDER_E2E";
const LIVE_E2E_URL = "GOBBY_LIVE_PROVIDER_E2E_URL";
const DEFAULT_CHAT_ROUTE = "/#chat";
const PROMPT_TIMEOUT_MS = 120_000;

interface ProviderMatrixModel {
  canonical_model: string;
  display_name: string;
  hidden?: boolean;
  is_default?: boolean;
}

interface ProviderModelEntry {
  provider: string;
  available: boolean;
  models: unknown[];
}

function isProviderMatrixModel(value: unknown): value is ProviderMatrixModel {
  if (typeof value !== "object" || value === null) return false;
  const model = value as Record<string, unknown>;
  return (
    typeof model.canonical_model === "string" &&
    typeof model.display_name === "string"
  );
}

function sanitizeToken(value: string): string {
  return value
    .replace(/[^a-z0-9]+/gi, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase();
}

function getLiveChatUrl(): string {
  return process.env[LIVE_E2E_URL] || DEFAULT_CHAT_ROUTE;
}

function liveProviderE2EIsDisabled(testInfo: TestInfo): boolean {
  if (process.env[LIVE_E2E_FLAG]) {
    return false;
  }

  testInfo.skip(
    true,
    `Set ${LIVE_E2E_FLAG}=1 to run real daemon-backed provider verification.`,
  );
  return true;
}

function setLiveTestBudget(testInfo: TestInfo, timeoutMs: number): void {
  testInfo.setTimeout(timeoutMs);
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
  request: APIRequestContext,
): Promise<Record<string, ProviderModelEntry>> {
  const authResponse = await request.get(getApiUrl("/api/auth/status"));
  expect(authResponse.ok()).toBeTruthy();
  const authStatus = await authResponse.json();
  expect(
    authStatus.auth_required && !authStatus.authenticated,
    "Live provider verification requires an authenticated daemon session.",
  ).toBe(false);

  const providersResponse = await request.get(
    getApiUrl("/api/providers/models"),
  );
  expect(providersResponse.ok()).toBeTruthy();
  const providersBody = await providersResponse.json();
  const providers = Array.isArray(providersBody?.providers)
    ? (providersBody.providers as ProviderModelEntry[])
    : [];

  return Object.fromEntries(providers.map((entry) => [entry.provider, entry]));
}

async function openFreshChat(
  page: Page,
  conversationId: string,
): Promise<void> {
  await page.goto(getLiveChatUrl());
  await page.evaluate((cid) => {
    localStorage.setItem("gobby-conversation-id", cid);
    localStorage.removeItem("gobby-db-session-id");
    localStorage.removeItem("gobby-selected-provider");
  }, conversationId);
  await page.reload();
  await expect(page.getByLabel("Select provider")).toBeVisible();
  await expect(
    page.getByRole("textbox", { name: /message input/i }),
  ).toBeVisible();
}

async function selectProviderModel(
  page: Page,
  provider: string,
  modelLabel: string,
): Promise<void> {
  await page.getByLabel("Select provider").click();
  const providerOption = page.getByRole("option", {
    name: getProviderDisplayName(provider),
    exact: true,
  });
  await expect(providerOption).toBeVisible();
  await providerOption.click();

  await page.getByLabel("Select model").click();
  const modelOption = page.getByRole("option", {
    name: modelLabel,
    exact: true,
  });
  await expect(modelOption).toBeVisible();
  await modelOption.click();
  await expect(
    page.getByRole("textbox", { name: /message input/i }),
  ).toBeVisible();
}

async function sendProbePrompt(
  page: Page,
  request: APIRequestContext,
  provider: string,
  model: ProviderMatrixModel,
): Promise<void> {
  const token = `live-${sanitizeToken(provider)}-${sanitizeToken(model.canonical_model)}`;
  const prompt = `Reply with exactly ${token} and nothing else.`;

  const input = page.getByRole("textbox", { name: /message input/i });
  await input.fill(prompt);
  await input.press("Enter");

  await page.waitForFunction(
    () => localStorage.getItem("gobby-db-session-id"),
    null,
    { timeout: 15_000 },
  );

  const dbSessionId = await page.evaluate(() =>
    localStorage.getItem("gobby-db-session-id"),
  );
  expect(dbSessionId).toBeTruthy();

  await expect
    .poll(
      async () => {
        const sessionResponse = await request.get(
          getApiUrl(`/api/sessions/${dbSessionId}`),
        );
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
    .toBe(`${provider}:${model.canonical_model}`);

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
        const messages = Array.isArray(messagesBody.messages)
          ? messagesBody.messages
          : [];
        return messages.some(
          (message: { role?: string; content?: string | null }) =>
            message.role === "assistant" &&
            (message.content || "").includes(token),
        );
      },
      { timeout: PROMPT_TIMEOUT_MS },
    )
    .toBe(true);
}

test.describe("Live provider picker verification", () => {
  test("Codex models can send and receive a live web chat response", async ({
    page,
    request,
  }, testInfo) => {
    setLiveTestBudget(testInfo, 10 * 60 * 1000);
    if (liveProviderE2EIsDisabled(testInfo)) {
      expect(process.env[LIVE_E2E_FLAG] ?? "").toBe("");
      return;
    }

    const catalog = await loadLiveCatalog(request);
    const runId = `live-run-${Date.now().toString(36)}`;
    const providersToVerify = ["codex"] as const;

    for (const providerName of providersToVerify) {
      const provider = catalog[providerName];
      expect(
        provider,
        `Provider ${providerName} must exist in /api/providers/models`,
      ).toBeTruthy();
      expect(
        provider.available,
        `Provider ${providerName} must be available`,
      ).toBeTruthy();
      const models = provider.models.filter(isProviderMatrixModel);
      expect(
        models.length,
        `Provider ${providerName} must expose matrix models`,
      ).toBeGreaterThan(0);
      const model =
        models.find((entry) => entry.is_default) ||
        models.find((entry) => !entry.hidden) ||
        models[0];

      await openFreshChat(
        page,
        `${runId}-${providerName}-${sanitizeToken(model.canonical_model)}`,
      );
      await selectProviderModel(page, providerName, model.display_name);
      await sendProbePrompt(page, request, providerName, model);
    }
  });
});
