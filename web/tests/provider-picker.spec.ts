import { expect, test } from "@playwright/test";

const CURRENT_CONVERSATION_ID = "web-provider-picker-conv";

function mockApiRoutes(page: Parameters<typeof test>[0]["page"]) {
  return page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();

    if (path === "/api/auth/status") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ auth_required: false, authenticated: true }),
      });
      return;
    }

    if (path === "/api/config/ui-settings") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body:
          method === "PUT"
            ? JSON.stringify({ ok: true })
            : JSON.stringify({
                selectedProjectId: "proj-1",
                model: "opus",
                theme: "dark",
                defaultChatMode: "plan",
                fontSize: 16,
              }),
      });
      return;
    }

    if (path === "/api/providers") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          providers: [
            { name: "claude", available: true },
            { name: "gemini", available: true },
            { name: "codex", available: true },
          ],
        }),
      });
      return;
    }

    if (path === "/api/providers/models") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          providers: [
            {
              provider: "claude",
              available: true,
              models: [
                {
                  value: "opus",
                  label: "Opus",
                  canonical_id: "claude-opus-4-6",
                },
                {
                  value: "sonnet",
                  label: "Sonnet",
                  canonical_id: "claude-sonnet-4-6",
                },
                {
                  value: "haiku",
                  label: "Haiku",
                  canonical_id: "claude-haiku-4-5-20251001",
                },
              ],
              source: "static",
            },
            {
                provider: "gemini",
                available: true,
                models: [
                { value: "gemini-3.1-pro-preview", label: "Gemini 3.1 Pro" },
                { value: "gemini-3-flash-preview", label: "Gemini 3 Flash" },
                ],
                source: "static",
              },
            {
              provider: "codex",
              available: true,
              models: [
                { value: "gpt-5.4", label: "GPT 5.4" },
                { value: "gpt-5.4-mini", label: "GPT 5.4 Mini" },
                { value: "gpt-5.3-codex", label: "GPT 5.3 Codex" },
                { value: "gpt-5.3-codex-spark", label: "GPT 5.3 Codex Spark" },
              ],
              source: "static",
            },
          ],
        }),
      });
      return;
    }

    if (path === "/api/voice/status") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ enabled: false, stt_available: false }),
      });
      return;
    }

    if (path === "/api/files/projects") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          { id: "proj-1", name: "Project One", repo_path: "/tmp/project-one" },
        ]),
      });
      return;
    }

    if (path === "/api/agents/running") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ agents: [] }),
      });
      return;
    }

    if (path === "/api/agents/definitions") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ definitions: [], global_defs: [], project_defs: [] }),
      });
      return;
    }

    if (path === "/api/sessions") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ sessions: [], total: 0 }),
      });
      return;
    }

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({}),
    });
  });
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(({ conversationId }: { conversationId: string }) => {
    localStorage.setItem("gobby-conversation-id", conversationId);
    localStorage.removeItem("gobby-db-session-id");
    localStorage.removeItem("gobby-selected-provider");
    localStorage.setItem(
      "gobby-settings",
      JSON.stringify({
        model: "opus",
        fontSize: 16,
        theme: "dark",
        defaultChatMode: "plan",
      }),
    );
  }, { conversationId: CURRENT_CONVERSATION_ID });

  await mockApiRoutes(page);
});

test("provider chip repairs an invalid persisted provider/model pair", async ({
  page,
}) => {
  await page.addInitScript(() => {
    localStorage.setItem("gobby-selected-provider", "claude");
    localStorage.setItem(
      "gobby-settings",
      JSON.stringify({
        model: "gpt-5.4",
        fontSize: 16,
        theme: "dark",
        defaultChatMode: "plan",
      }),
    );
  });

  await page.routeWebSocket("**/ws", (ws) => {
    ws.onMessage((raw) => {
      const msg = JSON.parse(String(raw)) as Record<string, unknown>;
      if (msg.type === "subscribe") {
        ws.send(
          JSON.stringify({
            type: "connection_established",
            conversation_ids: [CURRENT_CONVERSATION_ID],
          }),
        );
        ws.send(
          JSON.stringify({
            type: "subscribe_success",
            events: msg.events ?? [],
          }),
        );
      }
    });
  });

  await page.goto("/#chat");

  const providerButton = page.getByLabel("Select provider and model");
  const modelButton = page.getByLabel("Select model");
  await expect(providerButton).toBeVisible();
  await expect(providerButton).toContainText("Claude");
  await expect(modelButton).not.toContainText("GPT 5.4");
  await expect(modelButton).toContainText("Opus");
});

test("provider chip normalizes canonical model ids to the matching alias", async ({
  page,
}) => {
  await page.addInitScript(() => {
    localStorage.setItem("gobby-selected-provider", "claude");
    localStorage.setItem(
      "gobby-settings",
      JSON.stringify({
        model: "claude-opus-4-6",
        fontSize: 16,
        theme: "dark",
        defaultChatMode: "plan",
      }),
    );
  });

  await page.routeWebSocket("**/ws", (ws) => {
    ws.onMessage((raw) => {
      const msg = JSON.parse(String(raw)) as Record<string, unknown>;
      if (msg.type === "subscribe") {
        ws.send(
          JSON.stringify({
            type: "connection_established",
            conversation_ids: [CURRENT_CONVERSATION_ID],
          }),
        );
        ws.send(
          JSON.stringify({
            type: "subscribe_success",
            events: msg.events ?? [],
          }),
        );
      }
    });
  });

  await page.goto("/#chat");

  const providerButton = page.getByLabel("Select provider and model");
  const modelButton = page.getByLabel("Select model");
  await expect(providerButton).toBeVisible();
  await expect(providerButton).toContainText("Claude");
  await expect(modelButton).not.toContainText("Haiku");
  await expect(modelButton).toContainText("Opus");
});

test("Gemini picker selection sticks visually on a fresh chat", async ({
  page,
}) => {
  const outboundMessages: Array<Record<string, unknown>> = [];
  const conversationProviders = new Map<string, string>();

  await page.routeWebSocket("**/ws", (ws) => {
    ws.onMessage((raw) => {
      const msg = JSON.parse(String(raw)) as Record<string, unknown>;
      outboundMessages.push(msg);

      if (msg.type === "subscribe") {
        ws.send(
          JSON.stringify({
            type: "connection_established",
            conversation_ids: [CURRENT_CONVERSATION_ID],
          }),
        );
        ws.send(
          JSON.stringify({
            type: "subscribe_success",
            events: msg.events ?? [],
          }),
        );
        return;
      }

      if (msg.type === "set_provider") {
        conversationProviders.set(
          String(msg.conversation_id),
          String(msg.provider),
        );
        ws.send(
          JSON.stringify({
            type: "provider_switched",
            conversation_id: msg.conversation_id,
            old_provider: null,
            provider: msg.provider,
          }),
        );
        return;
      }

      if (msg.type === "set_agent") {
        ws.send(
          JSON.stringify({
            type: "agent_changed",
            conversation_id: msg.conversation_id,
            agent_name: msg.agent_name,
          }),
        );
        return;
      }

      if (msg.type === "chat_message") {
        const provider =
          (msg.provider as string | undefined) ??
          conversationProviders.get(String(msg.conversation_id)) ??
          "claude";

        ws.send(
          JSON.stringify({
            type: "session_info",
            conversation_id: msg.conversation_id,
            session_ref: "#501",
            agent_name: "default",
          }),
        );
        ws.send(
          JSON.stringify({
            type: "chat_stream",
            message_id: "assistant-provider-test",
            conversation_id: msg.conversation_id,
            request_id: msg.request_id,
            content: `${provider} reply`,
            done: false,
          }),
        );
        ws.send(
          JSON.stringify({
            type: "chat_stream",
            message_id: "assistant-provider-test",
            conversation_id: msg.conversation_id,
            request_id: msg.request_id,
            content: "",
            done: true,
          }),
        );
      }
    });
  });

  await page.goto("/#chat");
  await expect(page.getByRole("textbox", { name: /message input/i })).toBeVisible();

  await page.getByLabel("Select provider and model").click();
  await expect(page.getByRole("option", { name: "Gemini", exact: true })).toBeVisible();
  await expect(page.getByRole("option", { name: "Claude", exact: true })).toBeVisible();
  await expect(page.getByRole("option", { name: "Codex", exact: true })).toBeVisible();
  await page.getByRole("option", { name: "Gemini", exact: true }).click();
  await expect(page.getByLabel("Select provider and model")).toContainText("Gemini");

  await page.getByLabel("Select model").click();
  await expect(page.getByRole("option", { name: "Gemini 3.1 Pro", exact: true })).toBeVisible();
  await expect(page.getByRole("option", { name: "Gemini 3 Flash", exact: true })).toBeVisible();

  await page.screenshot({
    path: "tests/screenshots/provider-picker-initial.png",
    fullPage: true,
  });

  await page.getByRole("option", { name: "Gemini 3.1 Pro", exact: true }).click();

  await expect(page.getByLabel("Select provider and model")).toContainText("Gemini");
  await expect(page.getByLabel("Select model")).toContainText("Gemini 3.1 Pro");
  await expect
    .poll(() => page.evaluate(() => localStorage.getItem("gobby-selected-provider")))
    .toBe("gemini");

  await page.screenshot({
    path: "tests/screenshots/provider-picker-gemini-selected.png",
    fullPage: true,
  });

  await page.screenshot({
    path: "tests/screenshots/provider-picker-gemini-chat.png",
    fullPage: true,
  });
});

test("Codex picker shows friendly labels and no Default placeholder", async ({
  page,
}) => {
  const outboundMessages: Array<Record<string, unknown>> = [];

  await page.routeWebSocket("**/ws", (ws) => {
    ws.onMessage((raw) => {
      const msg = JSON.parse(String(raw)) as Record<string, unknown>;
      outboundMessages.push(msg);

      if (msg.type === "subscribe") {
        ws.send(
          JSON.stringify({
            type: "connection_established",
            conversation_ids: [CURRENT_CONVERSATION_ID],
          }),
        );
        ws.send(
          JSON.stringify({
            type: "subscribe_success",
            events: msg.events ?? [],
          }),
        );
        return;
      }

      if (msg.type === "set_provider") {
        ws.send(
          JSON.stringify({
            type: "provider_switched",
            conversation_id: msg.conversation_id,
            old_provider: null,
            provider: msg.provider,
          }),
        );
      }
    });
  });

  await page.goto("/#chat");
  await expect(page.getByRole("textbox", { name: /message input/i })).toBeVisible();

  await page.getByLabel("Select provider and model").click();
  await expect(page.getByRole("option", { name: "Codex", exact: true })).toBeVisible();
  await page.getByRole("option", { name: "Codex", exact: true }).click();
  await expect(page.getByLabel("Select provider and model")).toContainText("Codex");

  await page.getByLabel("Select model").click();
  await expect(page.getByRole("option", { name: "GPT 5.4", exact: true })).toBeVisible();
  await expect(page.getByRole("option", { name: "GPT 5.4 Mini", exact: true })).toBeVisible();
  await expect(page.getByRole("option", { name: "GPT 5.3 Codex", exact: true })).toBeVisible();
  await expect(page.getByRole("option", { name: "GPT 5.3 Codex Spark", exact: true })).toBeVisible();
  await expect(page.getByRole("option", { name: "Default", exact: true })).toHaveCount(0);

  await page.getByRole("option", { name: "GPT 5.4", exact: true }).click();

  await expect(page.getByLabel("Select provider and model")).toContainText("Codex");
  await expect(page.getByLabel("Select model")).toContainText("GPT 5.4");

  await page.screenshot({
    path: "tests/screenshots/provider-picker-codex-selected.png",
    fullPage: true,
  });
});
