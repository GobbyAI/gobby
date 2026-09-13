import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { TmuxSession } from "../../../../hooks/terminalRosterSnapshot";
import { TerminalSessionList } from "../TerminalSessionList";
import type { JoinedTerminalSession } from "../terminalSessions";

function makeTmux(overrides: Partial<TmuxSession> = {}): TmuxSession {
  const name = overrides.name ?? "shell";
  const socket = overrides.socket ?? "default";
  return {
    terminal_id: overrides.terminal_id ?? `${socket}:${name}`,
    backend: "tmux",
    ownership: socket === "gobby" ? "gobby" : "external",
    state: "live",
    title: name,
    session_id: overrides.session_id ?? null,
    agent_run_id: overrides.agent_run_id ?? null,
    dims: null,
    name,
    socket,
    pane_pid: 123,
    pane_dead: false,
    pane_title: null,
    pane_command: null,
    pane_path: null,
    window_name: null,
    session_title: null,
    gobby_session_id: null,
    agent_managed: false,
    attached_bridge: null,
    ...overrides,
  };
}

function makeJoined(
  overrides: Partial<JoinedTerminalSession> = {},
): JoinedTerminalSession {
  return {
    tmux: makeTmux(),
    gobby: null,
    label: "shell",
    refLabel: null,
    titleText: "shell",
    provider: null,
    paneRef: "default:shell",
    backendLabel: "tmux",
    dead: false,
    agentManaged: false,
    external: true,
    ...overrides,
  };
}

describe("TerminalSessionList kebab menu", () => {
  it("terminates the row's session through the shared kebab menu", async () => {
    const user = userEvent.setup();
    const onTerminate = vi.fn();
    const onChange = vi.fn();
    const gobbyManaged = makeJoined({
      tmux: makeTmux({ name: "worker", socket: "gobby" }),
      label: "worker",
      paneRef: "gobby:worker",
      external: false,
    });
    render(
      <TerminalSessionList
        sessions={[gobbyManaged]}
        value={null}
        onChange={onChange}
        onTerminate={onTerminate}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "Open actions for worker" }),
    );
    const menu = screen.getByRole("menu", { name: "Actions for worker" });
    await user.click(within(menu).getByRole("menuitem", { name: "Terminate" }));

    expect(onTerminate).toHaveBeenCalledTimes(1);
    expect(onTerminate).toHaveBeenCalledWith(gobbyManaged);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("disables Terminate for agent-managed sessions", async () => {
    const user = userEvent.setup();
    const onTerminate = vi.fn();
    render(
      <TerminalSessionList
        sessions={[
          makeJoined({
            tmux: makeTmux({ name: "agent-run", agent_managed: true }),
            label: "agent-run",
            agentManaged: true,
            external: false,
          }),
        ]}
        value={null}
        onChange={vi.fn()}
        onTerminate={onTerminate}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "Open actions for agent-run" }),
    );
    const menu = screen.getByRole("menu", { name: "Actions for agent-run" });
    const terminate = within(menu).getByRole("menuitem", { name: "Terminate" });
    expect(terminate).toBeDisabled();
    await user.click(terminate);
    expect(onTerminate).not.toHaveBeenCalled();
  });

  it("keeps the ref static, tickers the title, and pins chip + kebab to the row edge", () => {
    const longTitle = "x".repeat(120);
    const label = `#12: ${longTitle}`;
    render(
      <TerminalSessionList
        sessions={[
          makeJoined({
            tmux: makeTmux({ name: "agent-12" }),
            label,
            refLabel: "#12",
            titleText: longTitle,
            paneRef: "agent-12",
            external: false,
          }),
        ]}
        value={null}
        onChange={vi.fn()}
        onTerminate={vi.fn()}
      />,
    );

    const row = screen.getByRole("listitem");
    // The ref is a static mono meta slot; only the title tail lives in the
    // ticker, which owns the row's flexible width (gobby-#22205).
    expect(row.querySelector(".activity-row-meta")).toHaveTextContent("#12");
    const ticker = row.querySelector(".ticker");
    expect(ticker).toHaveClass("min-w-0", "flex-1");
    expect(ticker).toHaveTextContent(longTitle);
    // The raw tmux pane name is the terminal's identity, not the row's.
    expect(within(row).queryByText("agent-12")).toBeNull();
    // Chip and kebab share the row's last element so flex layout pins both
    // right with one gap between them.
    const trailing = row.lastElementChild;
    expect(trailing).toContainElement(
      screen.getByRole("button", { name: `Open actions for ${label}` }),
    );
    expect(trailing).toContainElement(within(row).getByText("tmux"));
  });

  it("keeps row selection independent of the kebab", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <TerminalSessionList
        sessions={[makeJoined()]}
        value={null}
        onChange={onChange}
        onTerminate={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Attach shell" }));
    expect(onChange).toHaveBeenCalledWith("default:shell");
  });
});

describe("TerminalSessionList backend chips", () => {
  it("names the backend on every row as tmux or gterm", () => {
    render(
      <TerminalSessionList
        sessions={[
          makeJoined({ label: "shell", backendLabel: "tmux" }),
          makeJoined({
            tmux: makeTmux({ name: "native-1", backend: "native" }),
            label: "native-1",
            paneRef: "tmux:native-1",
            backendLabel: "gterm",
          }),
        ]}
        value={null}
        onChange={vi.fn()}
        onTerminate={vi.fn()}
      />,
    );
    const rows = screen.getAllByRole("listitem");
    expect(within(rows[0]).getByText("tmux")).toBeInTheDocument();
    expect(within(rows[1]).getByText("gterm")).toBeInTheDocument();
  });
});
