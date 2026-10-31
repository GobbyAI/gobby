import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { GobbySession } from "../../../types/sessions";
import { SessionsEntryList } from "../SessionsTabList";
import { TerminalSessionList } from "../terminal/TerminalSessionList";
import { joinTmuxSessions } from "../terminal/terminalSessions";

function makeSession(status: string, index: number): GobbySession {
  return {
    id: `session-${index}`,
    ref: `#${index}`,
    external_id: `external-${index}`,
    source: "codex",
    project_id: "project-1",
    title: status,
    status,
    model: null,
    message_count: 0,
    created_at: "2026-10-31T00:00:00Z",
    updated_at: "2026-10-31T00:00:00Z",
    seq_num: index,
    summary_markdown: null,
    handoff_markdown: null,
    git_branch: null,
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: null,
    agent_run_id: null,
    parent_session_id: null,
    session_type: "terminal",
    terminal_context: null,
  };
}

function renderRows(
  surface: "Sessions" | "Terminal",
  statuses: string[],
): void {
  const sessions = statuses.map(makeSession);
  if (surface === "Sessions") {
    render(
      <SessionsEntryList
        entries={sessions.map((session) => ({
          id: session.id,
          type: "session",
          label: session.title ?? session.id,
          provider: session.source,
          status: session.status,
          inputTokens: 0,
          outputTokens: 0,
          totalTokens: 0,
          hasTmux: true,
          sandboxEnabled: false,
          isLocal: true,
        }))}
        isLoading={false}
        fetchError={null}
        emptyState={null}
        selectedSessionId={null}
        topHeight={50}
        onSelect={vi.fn()}
        onMenuButtonClick={vi.fn()}
      />,
    );
    return;
  }
  render(
    <TerminalSessionList
      sessions={joinTmuxSessions(
        sessions.map((session) => ({
          terminal_id: `terminal-${session.id}`,
          backend: "tmux",
          ownership: "gobby",
          state: "live",
          title: session.title,
          session_id: session.id,
          agent_run_id: null,
          dims: null,
          name: session.id,
          socket: "gobby",
          pane_pid: 123,
          pane_dead: false,
          pane_title: null,
          pane_command: null,
          pane_path: null,
          window_name: null,
          session_title: null,
          gobby_session_id: session.id,
          agent_managed: false,
          attached_bridge: null,
        })),
        sessions,
      )}
      value={null}
      onChange={vi.fn()}
      onTerminate={vi.fn()}
    />,
  );
}

function dot(status: string): HTMLElement {
  return screen.getByRole("img", { name: `Session ${status}` });
}

function glyphShape(status: string): string {
  const svg = dot(status).querySelector("svg");
  if (!svg) throw new Error(`No glyph for ${status}`);
  // Compare the actual SVG geometry, not a kind/class name. Distinct colors
  // alone cannot distinguish these states for a deutan or grayscale viewer.
  return svg.innerHTML;
}

// Each new or unrecognized lifecycle against every drawn status it could be
// confused with, including interrupted's dash glyph.
const NEW_LIFECYCLES = ["idle", "waiting", "future_status"];
const DRAWN_STATUSES = [
  "active",
  "paused",
  "interrupted",
  "awaiting_input",
  "awaiting_approval",
  "expired",
];
const GEOMETRY_PAIRS: [string, string][] = NEW_LIFECYCLES.flatMap((first) =>
  [...NEW_LIFECYCLES, ...DRAWN_STATUSES]
    .filter((second) => second !== first)
    .map((second): [string, string] => [first, second]),
);

describe.each(["Sessions", "Terminal"] as const)(
  "%s lifecycle dots",
  (surface) => {
    it.each(["idle", "waiting"])(
      "renders %s without an operator-hold dot",
      (status) => {
        renderRows(surface, [status, "paused"]);

        expect(dot(status)).not.toHaveAttribute("data-kind", "paused");
        expect(glyphShape(status)).not.toBe(glyphShape("paused"));
        expect(dot(status).style.color).toMatch(/^var\(--[a-z-]+\)$/);
      },
    );

    it("gives idle and waiting distinct shapes even without color", () => {
      renderRows(surface, ["idle", "waiting"]);

      expect(glyphShape("idle")).not.toBe(glyphShape("waiting"));
    });

    it.each(GEOMETRY_PAIRS)(
      "draws %s and %s with distinct geometry",
      (first, second) => {
        renderRows(surface, [first, second]);

        expect(glyphShape(first)).not.toBe(glyphShape(second));
      },
    );

    it("renders an unrecognized lifecycle as neutral rather than paused", () => {
      renderRows(surface, ["future_status", "paused"]);

      expect(dot("future_status")).not.toHaveAttribute("data-kind", "paused");
      expect(glyphShape("future_status")).not.toBe(glyphShape("paused"));
      expect([
        "var(--text-muted)",
        "var(--text-secondary)",
        "var(--color-inactive)",
      ]).toContain(dot("future_status").style.color);
    });

    it("preserves distinct operator pause and interruption glyphs", () => {
      renderRows(surface, ["paused", "interrupted"]);

      expect(dot("paused")).toHaveAttribute("data-kind", "paused");
      expect(glyphShape("interrupted")).not.toBe(glyphShape("paused"));
    });
  },
);

it("dims held Sessions rows while keeping idle and waiting rows legible", () => {
  renderRows("Sessions", ["idle", "waiting", "paused", "interrupted"]);

  for (const status of ["idle", "waiting"]) {
    expect(dot(status).closest(".session-entry")).not.toHaveClass(
      "session-entry--paused",
    );
  }
  for (const status of ["paused", "interrupted"]) {
    expect(dot(status).closest(".session-entry")).toHaveClass(
      "session-entry--paused",
    );
  }
});
