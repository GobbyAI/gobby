import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WikiTab } from "../WikiTab";

const statusPayload = {
  ok: true,
  payload: {
    scope: { project: "demo" },
    status: "ready",
    maintenance: {
      watcher: {
        active: true,
        running: true,
        scope_count: 2,
        last_index_time: 1767225600,
        pending_debounce: true,
        pending_changes: 3,
      },
      gateway: {
        available: true,
        status: "degraded",
        degraded: true,
        degraded_services: ["embeddings"],
        error: null,
      },
      degraded: true,
    },
    recent_searches: [{ query: "hooks", result_count: 3 }],
    indexed_paths: ["docs/wiki/hooks.md"],
    page_links: [{ title: "Hooks", url: "/wiki/hooks" }],
  },
};

const healthPayload = {
  ok: true,
  payload: {
    status: "degraded",
    degraded_services: ["embeddings"],
    findings: [{ severity: "warning", message: "Missing source asset", path: "raw/missing.md" }],
  },
};

const sourcesPayload = {
  ok: true,
  payload: {
    sources: [
      {
        id: "src-1",
        title: "Hooks source",
        path: "raw/hooks.md",
        wiki_path: "docs/wiki/hooks.md",
        page_url: "/wiki/hooks",
      },
    ],
  },
};

const previewPayload = {
  ok: true,
  payload: {
    command: "remove-source",
    dry_run: true,
    removed_paths: ["raw/hooks.md"],
    nested: { untouched_cli_key: "preserved" },
  },
};

const confirmedPayload = {
  ok: true,
  payload: {
    command: "remove-source",
    removed: true,
  },
};

describe("WikiTab", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/wiki/status")) {
        return Response.json(statusPayload);
      }
      if (url.includes("/api/wiki/health")) {
        return Response.json(healthPayload);
      }
      if (url.includes("/api/wiki/sources")) {
        return Response.json(sourcesPayload);
      }
      if (url.includes("/api/wiki/remove-source") && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        return Response.json(body.yes ? confirmedPayload : previewPayload);
      }
      return Response.json({ ok: false }, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders status, health, paths, source records, and links", async () => {
    render(<WikiTab projectId="demo" />);

    expect(await screen.findByText("ready")).toBeInTheDocument();
    expect(screen.getAllByText("degraded").length).toBeGreaterThan(0);
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByText("pending")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("2026-01-01T00:00:00.000Z")).toBeInTheDocument();
    expect(screen.getByText("available")).toBeInTheDocument();
    expect(screen.getByText("embeddings")).toBeInTheDocument();
    expect(screen.getByText("hooks (3)")).toBeInTheDocument();
    expect(screen.getAllByText("docs/wiki/hooks.md").length).toBeGreaterThan(0);
    expect(screen.getByText("raw/missing.md")).toBeInTheDocument();
    expect(screen.getAllByText("Hooks source").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "Hooks" })).toHaveAttribute("href", "/wiki/hooks");
  });

  it("source removal requires dry-run confirmation", async () => {
    const user = userEvent.setup();
    render(<WikiTab projectId="demo" />);

    await screen.findAllByText("Hooks source");
    await user.click(screen.getByRole("button", { name: /Remove\s+Hooks source/ }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/wiki/remove-source?project=demo"),
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ id: "src-1", dry_run: true }),
        }),
      );
    });

    const dialog = screen.getByRole("dialog", { name: "Remove wiki source" });
    expect(within(dialog).getByTestId("wiki-removal-preview")).toHaveTextContent(
      '"untouched_cli_key": "preserved"',
    );
    expect(within(dialog).getByLabelText("Keep source asset")).not.toBeChecked();

    await user.click(within(dialog).getByLabelText("Keep source asset"));
    await user.click(within(dialog).getByRole("button", { name: "Confirm removal" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/wiki/remove-source?project=demo"),
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ id: "src-1", yes: true, keep_asset: true }),
        }),
      );
    });
  });
});
