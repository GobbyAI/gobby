import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ACTIVITY_PANEL_TABS } from "../../ActivityPanelTabs";
import { IntegrationsTab } from "../../IntegrationsTab";
import { createMockFetch, type MockFetchInstance } from "../../../../test/mocks/fetch";
import {
  integrationPayloadFromDraft,
  validateIntegrationDraft,
} from "../IntegrationsTabModel";

vi.mock("../../../../hooks/useWebSocketEvent", () => ({
  useWebSocketEvent: vi.fn(),
}));

vi.mock("../../../chat/artifacts/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

type ChannelRecord = {
  id: string;
  channel_type: "slack" | "telegram" | "discord" | "teams" | "email" | "sms" | "gobby_chat";
  name: string;
  enabled: boolean;
  config_json: Record<string, unknown>;
  webhook_secret: string | null;
  created_at: string;
  updated_at: string;
};

type MessageRecord = {
  id: string;
  channel_id: string;
  identity_id: string | null;
  direction: "inbound" | "outbound";
  content: string;
  content_type: string;
  platform_message_id: string | null;
  platform_thread_id: string | null;
  session_id: string | null;
  status: string;
  error: string | null;
  metadata_json: Record<string, unknown>;
  created_at: string;
};

let mockFetch: MockFetchInstance;

function makeChannel(overrides: Partial<ChannelRecord>): ChannelRecord {
  return {
    id: "ch-default",
    channel_type: "slack",
    name: "Default channel",
    enabled: true,
    config_json: {},
    webhook_secret: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    ...overrides,
  };
}

function makeMessage(overrides: Partial<MessageRecord>): MessageRecord {
  return {
    id: "msg-default",
    channel_id: "ch-slack",
    identity_id: null,
    direction: "outbound",
    content: "Default message",
    content_type: "text",
    platform_message_id: null,
    platform_thread_id: null,
    session_id: null,
    status: "delivered",
    error: null,
    metadata_json: {},
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function setupFetch(channels: ChannelRecord[], messages: MessageRecord[] = []) {
  mockFetch = createMockFetch();
  mockFetch.fn.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const method = init?.method ?? "GET";

    if (url.endsWith("/api/comms/channels") && method === "GET") {
      return jsonResponse(channels);
    }
    if (url.endsWith("/api/comms/channels") && method === "POST") {
      return jsonResponse(
        makeChannel({
          id: "ch-new",
          channel_type: "telegram",
          name: "Ops pager",
          config_json: { chat_id: "-100123" },
        }),
      );
    }
    if (url.includes("/api/comms/messages")) {
      return jsonResponse(messages);
    }
    if (url.endsWith("/api/comms/channels/ch-slack/status")) {
      return jsonResponse({
        name: "release-alerts",
        channel_type: "slack",
        status: "active",
        active: true,
        enabled: true,
        supports_webhooks: true,
        supports_polling: false,
      });
    }
    if (url.endsWith("/api/comms/channels/ch-telegram/status")) {
      return jsonResponse({
        name: "incident-bridge",
        channel_type: "telegram",
        status: "inactive",
        active: false,
        enabled: false,
        supports_webhooks: true,
        supports_polling: true,
        is_polling: false,
      });
    }
    if (url.endsWith("/api/comms/channels/ch-slack") && method === "PUT") {
      return jsonResponse({
        ...channels[0],
        enabled: false,
        config_json: { channel_id: "C999" },
      });
    }
    if (url.endsWith("/api/comms/channels/ch-slack") && method === "DELETE") {
      return jsonResponse({ status: "ok" });
    }
    return jsonResponse({ error: "no mock route matched" }, 404);
  });
}

function setupFetchFailure(status: number) {
  mockFetch = createMockFetch();
  mockFetch.fn.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const method = init?.method ?? "GET";
    if (url.endsWith("/api/comms/channels") && method === "GET") {
      return jsonResponse({ error: "communications unavailable" }, status);
    }
    return jsonResponse({ error: "no mock route matched" }, 404);
  });
}

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function lastJsonBodyFor(pathPart: string) {
  const call = mockFetch.fn.mock.calls
    .slice()
    .reverse()
    .find(([url, init]) => {
      const requestInit = init as RequestInit | undefined;
      return String(url).includes(pathPart) && Boolean(requestInit?.body);
    });
  const init = call?.[1] as RequestInit | undefined;
  return init?.body ? JSON.parse(String(init.body)) : null;
}

describe("Integrations activity tab", () => {
  afterEach(() => {
    mockFetch?.restore();
    vi.restoreAllMocks();
  });

  it("registers the tab and filters channel rows from the toolbar", async () => {
    setupFetch([
      makeChannel({
        id: "ch-slack",
        name: "Release alerts",
        channel_type: "slack",
        config_json: { channel_id: "C123" },
      }),
      makeChannel({
        id: "ch-telegram",
        name: "Incident bridge",
        channel_type: "telegram",
        enabled: false,
        config_json: { chat_id: "-100123" },
      }),
    ]);

    const user = userEvent.setup();

    expect(ACTIVITY_PANEL_TABS.some((tab) => tab.id === "integrations")).toBe(true);
    render(<IntegrationsTab />);

    expect(
      await screen.findByRole("button", { name: "Select Release alerts" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Select Incident bridge" })).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Search integrations" })).toHaveAttribute(
      "name",
      "search-integrations",
    );
    expect(screen.getByRole("combobox", { name: "Platform filter" })).toHaveAttribute(
      "name",
      "integration-platform-filter",
    );
    expect(screen.getByRole("combobox", { name: "Integration status" })).toHaveAttribute(
      "name",
      "integration-status-filter",
    );

    const incident = screen.getByRole("button", { name: "Select Incident bridge" });
    incident.focus();
    await user.keyboard("{Enter}");
    expect(incident.parentElement).toHaveClass("activity-list-row--selected");

    await user.selectOptions(screen.getByRole("combobox", { name: "Platform filter" }), "telegram");
    expect(screen.queryByText("Release alerts")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Select Incident bridge" })).toBeInTheDocument();

    await user.clear(screen.getByRole("searchbox", { name: "Search integrations" }));
    await user.type(screen.getByRole("searchbox", { name: "Search integrations" }), "incident");
    expect(screen.getByRole("button", { name: "Select Incident bridge" })).toBeInTheDocument();
  });

  it("exposes row actions through the shared kebab menu", async () => {
    setupFetch([
      makeChannel({
        id: "ch-slack",
        name: "Release alerts",
        channel_type: "slack",
        config_json: { channel_id: "C123" },
      }),
    ]);

    const user = userEvent.setup();
    render(<IntegrationsTab />);

    expect(
      await screen.findByRole("button", { name: "Select Release alerts" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open actions for Release alerts" }));

    const menu = screen.getByRole("menu", { name: "Actions for Release alerts" });
    expect(within(menu).getByRole("menuitem", { name: "Disable" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "Delete" })).toBeInTheDocument();

    await user.click(within(menu).getByRole("menuitem", { name: "Disable" }));
    await waitFor(() =>
      expect(lastJsonBodyFor("/api/comms/channels/ch-slack")).toEqual({ enabled: false }),
    );
  });

  it("saves selected channel drafts explicitly and keeps Discard available", async () => {
    setupFetch([
      makeChannel({
        id: "ch-slack",
        name: "Release alerts",
        channel_type: "slack",
        config_json: { channel_id: "C123" },
      }),
    ]);

    const user = userEvent.setup();
    render(<IntegrationsTab />);

    await user.click(await screen.findByRole("button", { name: "Select Release alerts" }));
    expect(await screen.findByRole("heading", { name: "Release alerts" })).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Channel ID"));
    await user.type(screen.getByLabelText("Channel ID"), "C999");

    expect(screen.getByRole("button", { name: "Discard" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(lastJsonBodyFor("/api/comms/channels/ch-slack")).toEqual({
        config: { channel_id: "C999" },
        enabled: true,
      }),
    );
  });

  it("sends changed secrets when saving an existing channel", async () => {
    setupFetch([
      makeChannel({
        id: "ch-slack",
        name: "Release alerts",
        channel_type: "slack",
        config_json: {
          channel_id: "C123",
          bot_token: "$secret:COMMS_SLACK_BOT_TOKEN_RELEASE_ALERTS",
        },
      }),
    ]);

    const user = userEvent.setup();
    render(<IntegrationsTab />);

    await user.click(await screen.findByRole("button", { name: "Select Release alerts" }));
    await user.type(screen.getByLabelText("Bot Token"), "new-token");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(lastJsonBodyFor("/api/comms/channels/ch-slack")).toEqual({
        config: { channel_id: "C123" },
        enabled: true,
        secrets: { bot_token: "new-token" },
      }),
    );
  });

  it("encodes channel names in webhook URLs", async () => {
    setupFetch([
      makeChannel({
        id: "ch-slack",
        name: "Release alerts/primary",
        channel_type: "slack",
        config_json: { channel_id: "C123" },
      }),
    ]);

    const user = userEvent.setup();
    render(<IntegrationsTab />);
    await user.click(
      await screen.findByRole("button", { name: "Select Release alerts/primary" }),
    );

    expect(
      await screen.findByText(
        `${window.location.origin}/api/comms/webhooks/Release%20alerts%2Fprimary`,
      ),
    ).toBeInTheDocument();
  });

  it("swaps the selected channel detail pane to recent messages", async () => {
    setupFetch(
      [
        makeChannel({
          id: "ch-slack",
          name: "Release alerts",
          channel_type: "slack",
          config_json: { channel_id: "C123" },
        }),
      ],
      [
        makeMessage({
          id: "msg-release",
          content: "Release shipped",
          direction: "outbound",
          status: "delivered",
          created_at: "2026-01-02T00:00:00Z",
        }),
      ],
    );

    const user = userEvent.setup();
    render(<IntegrationsTab />);

    await user.click(await screen.findByRole("button", { name: "Select Release alerts" }));
    await user.click(screen.getByRole("button", { name: "Messages" }));

    expect(await screen.findByRole("heading", { name: "Messages" })).toBeInTheDocument();
    expect(screen.getByText("Release shipped")).toBeInTheDocument();
    expect(
      mockFetch.fn.mock.calls.some(([url]) =>
        String(url).includes("/api/comms/messages?channel_id=ch-slack"),
      ),
    ).toBe(true);

    await user.click(screen.getByRole("button", { name: "Close messages" }));
    expect(await screen.findByRole("heading", { name: "Release alerts" })).toBeInTheDocument();
  });

  it("creates channels with config fields separated from secrets", async () => {
    setupFetch([]);

    const user = userEvent.setup();
    render(<IntegrationsTab />);

    expect(await screen.findByRole("button", { name: "+ Channel" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "+ Channel" }));
    await user.selectOptions(screen.getByLabelText("Platform"), "telegram");
    await user.type(screen.getByLabelText("Name"), "Ops pager");
    expect(screen.getByLabelText("Bot Token")).toHaveAttribute(
      "name",
      "integration-secret-bot_token",
    );
    await user.type(screen.getByLabelText("Bot Token"), "telegram-token");
    await user.type(screen.getByLabelText("Chat ID"), "-100123");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(lastJsonBodyFor("/api/comms/channels")).toEqual({
        channel_type: "telegram",
        name: "Ops pager",
        config: { chat_id: "-100123" },
        secrets: { bot_token: "telegram-token" },
      }),
    );
  });

  it("renders communications setup state separately from zero channels on 503", async () => {
    setupFetchFailure(503);

    render(<IntegrationsTab />);

    expect(await screen.findByText("Communications not configured")).toBeInTheDocument();
    expect(
      screen.getByText("Start the communications manager before managing notification channels."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Communication channels appear here after they are configured."),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Dismiss error: Request failed: 503" }),
    ).not.toBeInTheDocument();
  });

  it("renders communications setup state when the feature is disabled (404)", async () => {
    setupFetchFailure(404);

    render(<IntegrationsTab />);

    expect(await screen.findByText("Communications not configured")).toBeInTheDocument();
    expect(
      screen.getByText("Start the communications manager before managing notification channels."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Communication channels appear here after they are configured."),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Dismiss error: Request failed: 404" }),
    ).not.toBeInTheDocument();
  });
});

describe("validateIntegrationDraft", () => {
  it("rejects non-empty non-finite numeric config fields", () => {
    expect(
      validateIntegrationDraft({
        id: null,
        mode: "create",
        name: "Email bridge",
        channel_type: "email",
        enabled: true,
        config: {
          smtp_host: "smtp.example.com",
          smtp_port: "Infinity",
          imap_host: "imap.example.com",
          imap_port: "993",
          from_address: "ops@example.com",
        },
        secrets: { password: "secret" },
      }),
    ).toBe("SMTP Port must be a finite number");
  });

  it("rejects non-finite numeric config while building a payload", () => {
    expect(() =>
      integrationPayloadFromDraft({
        id: null,
        mode: "create",
        name: "Email bridge",
        channel_type: "email",
        enabled: true,
        config: {
          smtp_host: "smtp.example.com",
          smtp_port: "Infinity",
          imap_host: "imap.example.com",
          imap_port: "993",
          from_address: "ops@example.com",
        },
        secrets: { password: "secret" },
      }),
    ).toThrow("SMTP Port must be a finite number");
  });
});
