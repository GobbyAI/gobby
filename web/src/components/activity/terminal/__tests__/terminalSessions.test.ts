import { describe, expect, it } from "vitest";

import type { TmuxSession } from "../../../../hooks/useTmuxSessions";
import type { GobbySession } from "../../../../types/sessions";
import {
  findByGobbySessionId,
  joinTmuxSessions,
  sessionKey,
} from "../terminalSessions";

function makeGobbySession(overrides: Partial<GobbySession> = {}): GobbySession {
  return {
    id: "session-1",
    ref: "#1",
    external_id: "external-1",
    source: "codex",
    project_id: "project-1",
    title: "Session",
    status: "active",
    model: "gpt-5.4",
    message_count: 0,
    created_at: "2026-05-01T00:00:00Z",
    updated_at: "2026-05-01T00:00:00Z",
    seq_num: 1,
    summary_markdown: null,
    digest_markdown: null,
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
    ...overrides,
  };
}

function makeTmuxSession(overrides: Partial<TmuxSession> = {}): TmuxSession {
  const name = overrides.name ?? "shell";
  const socket = overrides.socket ?? "default";
  return {
    terminal_id: overrides.terminal_id ?? `${socket}:${name}`,
    backend: overrides.backend ?? "tmux",
    ownership: overrides.ownership ?? (socket === "gobby" ? "gobby" : "external"),
    state: overrides.state ?? "live",
    title: overrides.title ?? name,
    session_id: overrides.session_id ?? overrides.gobby_session_id ?? null,
    agent_run_id: overrides.agent_run_id ?? null,
    dims: overrides.dims ?? null,
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

describe("terminal session helpers", () => {
  it("joins tmux and gobby sessions", () => {
    const userSession = makeGobbySession({
      id: "user-session",
      seq_num: 7,
      title: "User shell",
    });
    const agentSession = makeGobbySession({
      id: "agent-session",
      seq_num: 8,
      title: "Agent shell",
      agent_run_id: "run-8",
    });
    const tmux = [
      makeTmuxSession({
        name: "user-shell",
        gobby_session_id: userSession.id,
        agent_managed: true,
      }),
      makeTmuxSession({ name: "external-shell", pane_dead: true }),
      makeTmuxSession({
        name: "agent-shell",
        socket: "gobby",
        gobby_session_id: "wrong-session",
        agent_run_id: agentSession.agent_run_id,
      }),
    ];

    const joined = joinTmuxSessions(tmux, [userSession, agentSession]);

    expect(sessionKey(tmux[0])).toBe("default:user-shell");
    expect(joined).toEqual([
      {
        tmux: tmux[0],
        gobby: userSession,
        label: "#7 User shell",
        provider: "codex",
        paneRef: "default:user-shell",
        dead: false,
        agentManaged: false,
        external: false,
      },
      {
        tmux: tmux[1],
        gobby: null,
        label: "external-shell",
        provider: null,
        paneRef: "default:external-shell",
        dead: true,
        agentManaged: false,
        external: true,
      },
      {
        tmux: tmux[2],
        gobby: agentSession,
        label: "#8 Agent shell",
        provider: "codex",
        paneRef: "gobby:agent-shell",
        dead: false,
        agentManaged: true,
        external: false,
      },
    ]);
    expect(findByGobbySessionId(joined, agentSession.id)).toBe(joined[2]);
    expect(findByGobbySessionId(joined, "missing")).toBeNull();
    expect(joinTmuxSessions([tmux[1]], undefined)[0].external).toBe(true);
  });

  it("socket specific identity join", () => {
    const userSession = makeGobbySession({
      id: "user-session",
      seq_num: 10,
      title: "User",
    });
    const agentSession = makeGobbySession({
      id: "agent-session",
      seq_num: 11,
      title: "Agent",
      agent_run_id: "run-11",
    });
    const tmux = [
      makeTmuxSession({
        name: "same-name",
        socket: "default",
        gobby_session_id: userSession.id,
        agent_run_id: agentSession.agent_run_id,
        agent_managed: true,
      }),
      makeTmuxSession({
        name: "same-name",
        socket: "gobby",
        gobby_session_id: null,
        session_id: null,
        agent_run_id: agentSession.agent_run_id,
      }),
      makeTmuxSession({
        name: "pane-id-collision",
        socket: "default",
        gobby_session_id: null,
        session_id: null,
        agent_run_id: null,
        agent_managed: true,
      }),
    ];

    const joined = joinTmuxSessions(tmux, [userSession, agentSession]);

    expect(joined.map(({ tmux: row }) => sessionKey(row))).toEqual([
      "default:same-name",
      "gobby:same-name",
      "default:pane-id-collision",
    ]);
    expect(joined[0].gobby).toBe(userSession);
    expect(joined[0].agentManaged).toBe(false);
    expect(joined[1].gobby).toBe(agentSession);
    expect(joined[1].agentManaged).toBe(true);
    expect(joined[2].label).toBe("pane-id-collision");
    expect(findByGobbySessionId(joined, agentSession.id)).toBe(joined[1]);
  });

  it("titles unmanaged panes by running app, then cwd, then tmux name", () => {
    const tmux = [
      makeTmuxSession({
        name: "0",
        pane_command: "vim",
        pane_path: "/Users/dev/notes",
      }),
      makeTmuxSession({
        name: "1",
        pane_command: "zsh",
        pane_path: "/Users/dev/projects/gobby",
      }),
      makeTmuxSession({ name: "2", pane_command: "-bash", pane_path: null }),
    ];

    const joined = joinTmuxSessions(tmux, []);

    expect(joined[0].label).toBe("vim");
    expect(joined[1].label).toBe("gobby");
    expect(joined[2].label).toBe("2");
    expect(joined.map(({ paneRef }) => paneRef)).toEqual([
      "default:0",
      "default:1",
      "default:2",
    ]);
  });

  it("derives the provider icon from the pane command for unmanaged panes", () => {
    const joined = joinTmuxSessions(
      [
        makeTmuxSession({
          name: "0",
          pane_command: "claude",
          pane_path: "/tmp",
        }),
        makeTmuxSession({ name: "1", pane_command: "htop", pane_path: "/tmp" }),
      ],
      [],
    );

    expect(joined[0].provider).toBe("claude");
    expect(joined[0].label).toBe("claude");
    expect(joined[1].provider).toBeNull();
  });

  it("titles agent-managed panes without a joined session by tmux name", () => {
    const joined = joinTmuxSessions(
      [
        makeTmuxSession({
          name: "gobby-agent-refactor",
          socket: "gobby",
          agent_managed: true,
          agent_run_id: "run-unknown",
          pane_command: "node",
          pane_path: "/Users/dev/projects/gobby",
        }),
      ],
      [],
    );

    expect(joined[0].label).toBe("gobby-agent-refactor");
    expect(joined[0].external).toBe(true);
  });
});
