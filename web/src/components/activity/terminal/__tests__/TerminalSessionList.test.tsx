import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { TmuxSession } from "../../../../hooks/useTmuxSessions";
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
    provider: null,
    paneRef: "default:shell",
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

  it("bounds long labels and paneRefs so the kebab stays pinned to the row edge", () => {
    const longName = "x".repeat(120);
    render(
      <TerminalSessionList
        sessions={[
          makeJoined({
            tmux: makeTmux({ name: longName }),
            label: longName,
            paneRef: `default:${longName}`,
          }),
        ]}
        value={null}
        onChange={vi.fn()}
        onTerminate={vi.fn()}
      />,
    );

    const row = screen.getByRole("listitem");
    // The label lives in the shared truncating title slot and the paneRef
    // meta carries its own cap — the shared idiom marks meta shrink-0, so an
    // uncapped tmux name would push the kebab off the row (gobby-#20064).
    const title = row.querySelector(".activity-row-title");
    expect(title).toHaveTextContent(longName);
    const meta = row.querySelector(".activity-row-meta");
    expect(meta).toHaveClass("truncate", "max-w-[45%]");
    // Kebab wrapper is the row's last element so flex layout pins it right.
    const kebab = screen.getByRole("button", {
      name: `Open actions for ${longName}`,
    });
    expect(row.lastElementChild).toContainElement(kebab);
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
