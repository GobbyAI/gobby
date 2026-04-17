import { expect, test } from "@playwright/test";
import { promises as fs } from "node:fs";

import {
  getModelsForProvider,
  getProviderDisplayName,
  type ProviderModelEntry,
  type ProviderModelOption,
} from "../src/lib/providerModels";

const LIVE_E2E_FLAG = "GOBBY_LIVE_PROVIDER_E2E";
const LIVE_E2E_URL = "GOBBY_LIVE_PROVIDER_E2E_URL";
const LIVE_PROVIDER_FILTER = "GOBBY_APPROVAL_PROVIDERS";
const DEFAULT_CHAT_ROUTE = "/#chat";
const PROMPT_TIMEOUT_MS = 180_000;

interface SessionSummary {
  id: string;
  ref: string;
  source: string;
  model: string;
  session_type?: string | null;
  agent_run_id?: string | null;
}

interface SessionListEntry {
  id: string;
  source: string;
  status: string;
  session_type?: string | null;
  agent_run_id?: string | null;
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

async function fileContents(filePath: string): Promise<string | null> {
  try {
    return await fs.readFile(filePath, "utf8");
  } catch {
    return null;
  }
}

async function removeFile(filePath: string): Promise<void> {
  try {
    await fs.unlink(filePath);
  } catch {
    // File is best-effort cleanup for the verification flow.
  }
}

async function loadLiveCatalog(
  request: Parameters<typeof test>[0]["request"],
): Promise<Record<string, ProviderModelEntry>> {
  const authResponse = await request.get(getApiUrl("/api/auth/status"));
  expect(authResponse.ok()).toBeTruthy();
  const authStatus = await authResponse.json();
  test.skip(
    authStatus.auth_required && !authStatus.authenticated,
    "Live provider approval verification requires an authenticated daemon session.",
  );

  const providersResponse = await request.get(getApiUrl("/api/providers/models"));
  expect(providersResponse.ok()).toBeTruthy();
  const providersBody = await providersResponse.json();
  const providers = Array.isArray(providersBody?.providers)
    ? (providersBody.providers as ProviderModelEntry[])
    : [];

  return Object.fromEntries(providers.map((entry) => [entry.provider, entry]));
}

function pickModel(provider: ProviderModelEntry): ProviderModelOption {
  const candidates = getModelsForProvider([provider], provider.provider);

  const findValue = (value: string): ProviderModelOption | undefined =>
    candidates.find((entry) => entry.value === value);

  switch (provider.provider) {
    case "claude":
      return findValue("haiku") ?? candidates[0];
    case "gemini":
      return (
        findValue("gemini-2.5-flash-lite") ??
        findValue("gemini-2.5-flash") ??
        candidates.find((entry) => !entry.value.startsWith("auto-")) ??
        candidates[0]
      );
    case "codex":
      return (
        findValue("gpt-5.4-mini") ??
        findValue("gpt-5.1-codex-mini") ??
        candidates.find((entry) => entry.is_default) ??
        candidates[0]
      );
    default:
      return candidates[0];
  }
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
    localStorage.removeItem("gobby-viewing-session-id");
    localStorage.removeItem("gobby-viewing-session-mode");
  }, conversationId);
  await page.reload();
  await expect(page.getByLabel("Select provider and model")).toBeVisible();
  await expect(page.getByRole("textbox", { name: /message input/i })).toBeVisible();
}

async function selectProviderAndModel(
  page: Parameters<typeof test>[0]["page"],
  provider: string,
  modelLabel: string,
): Promise<void> {
  await page.getByLabel("Select provider and model").click();
  const providerOption = page.getByRole("option", {
    name: getProviderDisplayName(provider),
    exact: true,
  });
  await expect(providerOption).toBeVisible();
  await providerOption.click();

  await page.getByLabel("Select model").click();
  const modelOption = page.getByRole("option", { name: modelLabel, exact: true });
  await expect(modelOption).toBeVisible();
  await modelOption.click();
  await expect(page.getByRole("textbox", { name: /message input/i })).toBeVisible();
}

async function selectMode(
  page: Parameters<typeof test>[0]["page"],
  modeLabel: "Act" | "Auto",
): Promise<void> {
  const radio = page.getByRole("radio", { name: modeLabel, exact: true });
  await radio.click();
  await expect(radio).toHaveAttribute("aria-checked", "true");
}

async function waitForSessionSummary(
  request: Parameters<typeof test>[0]["request"],
  dbSessionId: string,
  provider: string,
  model: string,
): Promise<void> {
  await expect
    .poll(
      async () => {
        const response = await request.get(getApiUrl(`/api/sessions/${dbSessionId}`));
        if (!response.ok()) {
          return null;
        }
        const body = await response.json();
        const session = body?.session as SessionSummary | undefined;
        if (!session?.id || !session?.ref) {
          return null;
        }
        if (session.source !== provider || session.model !== model) {
          return null;
        }
        return session;
      },
      { timeout: PROMPT_TIMEOUT_MS },
    )
    .toBeTruthy();
}

async function sendPrompt(
  page: Parameters<typeof test>[0]["page"],
  prompt: string,
): Promise<string> {
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
  return dbSessionId!;
}

function buildCommandPrompt(filePath: string, token: string): string {
  const safeToken = sanitizeToken(token);
  return [
    "Use a shell or terminal tool to execute this exact command:",
    `printf '%s' '${safeToken}' > '${filePath}'`,
    `After the command succeeds, reply with exactly ${safeToken} and nothing else.`,
    "Do not answer from memory. You must actually use a tool.",
  ].join("\n");
}

async function verifyActModeApproval(
  page: Parameters<typeof test>[0]["page"],
  request: Parameters<typeof test>[0]["request"],
  provider: string,
  model: ProviderModelEntry["models"][number],
  runId: string,
): Promise<void> {
  const token = `act-${sanitizeToken(provider)}-${runId}`;
  const filePath = `/tmp/gobby-approval-${sanitizeToken(provider)}-${runId}-act.txt`;
  await removeFile(filePath);

  await openFreshChat(page, `${runId}-${sanitizeToken(provider)}-act`);
  await selectProviderAndModel(page, provider, model.label);
  await selectMode(page, "Act");

  const dbSessionId = await sendPrompt(page, buildCommandPrompt(filePath, token));
  await waitForSessionSummary(request, dbSessionId, provider, model.value);

  await expect(page.getByText("Approval Required")).toBeVisible({
    timeout: PROMPT_TIMEOUT_MS,
  });
  expect(await fileContents(filePath)).toBeNull();

  await page.getByRole("button", { name: "Approve", exact: true }).click();

  await expect
    .poll(async () => await fileContents(filePath), { timeout: PROMPT_TIMEOUT_MS })
    .toBe(token);
  await expect(page.getByText("Approval Required")).toHaveCount(0, {
    timeout: PROMPT_TIMEOUT_MS,
  });
  await expect(page.getByText("Generation failed")).toHaveCount(0);

  await removeFile(filePath);
}

async function verifyAutoModeSuppression(
  page: Parameters<typeof test>[0]["page"],
  request: Parameters<typeof test>[0]["request"],
  provider: string,
  model: ProviderModelEntry["models"][number],
  runId: string,
): Promise<void> {
  const token = `auto-${sanitizeToken(provider)}-${runId}`;
  const filePath = `/tmp/gobby-approval-${sanitizeToken(provider)}-${runId}-auto.txt`;
  await removeFile(filePath);

  await openFreshChat(page, `${runId}-${sanitizeToken(provider)}-auto`);
  await selectProviderAndModel(page, provider, model.label);
  await selectMode(page, "Auto");

  const dbSessionId = await sendPrompt(page, buildCommandPrompt(filePath, token));
  await waitForSessionSummary(request, dbSessionId, provider, model.value);

  await expect
    .poll(async () => await fileContents(filePath), { timeout: PROMPT_TIMEOUT_MS })
    .toBe(token);
  await expect(page.getByText("Approval Required")).toHaveCount(0);
  await expect(page.getByText("Generation failed")).toHaveCount(0);

  await removeFile(filePath);
}

async function loadInteractiveTerminalSession(
  request: Parameters<typeof test>[0]["request"],
): Promise<SessionListEntry> {
  const response = await request.get(getApiUrl("/api/sessions?limit=50&offset=0"));
  expect(response.ok()).toBeTruthy();
  const body = await response.json();
  const sessions = Array.isArray(body?.sessions) ? (body.sessions as SessionListEntry[]) : [];
  const terminalSession = sessions.find(
    (session) => session.session_type === "terminal" && !session.agent_run_id,
  );

  expect(terminalSession, "Expected at least one interactive terminal session").toBeTruthy();
  return terminalSession!;
}

test.describe("Live approval mode verification", () => {
  test.skip(
    !process.env[LIVE_E2E_FLAG],
    `Set ${LIVE_E2E_FLAG}=1 to run real daemon-backed approval verification.`,
  );

  test("Claude, Gemini, Qwen, and Codex prompt in Act and suppress prompts in Auto", async ({
    page,
    request,
  }) => {
    test.setTimeout(20 * 60 * 1000);

    const catalog = await loadLiveCatalog(request);
    const requestedProviders = (process.env[LIVE_PROVIDER_FILTER] || "")
      .split(",")
      .map((entry) => entry.trim().toLowerCase())
      .filter(Boolean);
    const providersToVerify = (
      requestedProviders.length > 0
        ? requestedProviders
        : ["claude", "gemini", "qwen", "codex"]
    ) as Array<"claude" | "gemini" | "qwen" | "codex">;
    const runId = Date.now().toString(36);

    for (const providerName of providersToVerify) {
      const provider = catalog[providerName];
      expect(provider, `Provider ${providerName} must exist in /api/providers/models`).toBeTruthy();
      expect(provider.available, `Provider ${providerName} must be available`).toBeTruthy();
      expect(provider.models.length, `Provider ${providerName} must expose models`).toBeGreaterThan(
        0,
      );

      const model = pickModel(provider);
      await test.step(`${providerName} Act mode prompts before execution`, async () => {
        await verifyActModeApproval(page, request, providerName, model, runId);
      });
      await test.step(`${providerName} Auto mode suppresses approval prompts`, async () => {
        await verifyAutoModeSuppression(page, request, providerName, model, runId);
      });
    }
  });

  test("observing a terminal session stays read-only in the web chat UI", async ({
    page,
    request,
  }) => {
    test.setTimeout(2 * 60 * 1000);

    const terminalSession = await loadInteractiveTerminalSession(request);
    await page.goto(getLiveChatUrl());
    await page.evaluate((sessionId) => {
      localStorage.setItem("gobby-conversation-id", `live-terminal-view-${Date.now()}`);
      localStorage.removeItem("gobby-db-session-id");
      localStorage.setItem("gobby-viewing-session-id", sessionId);
      localStorage.setItem("gobby-viewing-session-mode", "observe");
    }, terminalSession.id);
    await page.reload();

    await expect(page.getByTestId("agent-status-bar")).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.getByRole("textbox", { name: /message input/i })).toHaveCount(0);
    await expect(page.getByRole("radiogroup", { name: "Chat mode" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Attach" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Resume" })).toBeVisible();
  });
});
