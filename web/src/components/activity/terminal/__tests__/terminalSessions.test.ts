import { describe, expect, it } from "vitest";

import type { TerminalSession } from "../../../../hooks/terminalRosterSnapshot";
import type { SessionRosterLabel } from "../../../../lib/sessionLabel";
import type { GobbySession } from "../../../../types/sessions";
import {
  findByGobbySessionId,
  joinTerminalSessions,
  sessionKey,
} from "../terminalSessions";

function makeGobbySession(overrides: Partial<GobbySession> = {}): GobbySession {
  return {
    id: "session-1",
    ref: "gobby#1",
    external_id: "external-1",
    source: "codex",
    project_id: "project-1",
    status: "active",
    model: "gpt-5.4",
    message_count: 0,
    created_at: "2026-05-01T00:00:00Z",
    updated_at: "2026-05-01T00:00:00Z",
    seq_num: 1,
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
    ...overrides,
  };
}

function makeTerminalSession(
  overrides: Partial<TerminalSession> = {},
): TerminalSession {
  const name = overrides.name ?? "shell";
  const socket = overrides.socket ?? "default";
  return {
    terminal_id: overrides.terminal_id ?? `${socket}:${name}`,
    backend: overrides.backend ?? "native",
    ownership:
      overrides.ownership ?? (socket === "gobby" ? "gobby" : "external"),
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
    gobby_session_id: null,
    agent_managed: false,
    attached_bridge: null,
    ...overrides,
  };
}

describe("terminal session helpers", () => {
  it("joins terminal and gobby sessions", () => {
    const userSession = makeGobbySession({
      id: "user-session",
      seq_num: 7,
      ref: "other-project#7",
    });
    const agentSession = makeGobbySession({
      id: "agent-session",
      seq_num: 8,
      ref: "gobby#8",
      agent_run_id: "run-8",
    });
    const terminal = [
      makeTerminalSession({
        name: "user-shell",
        gobby_session_id: userSession.id,
        agent_managed: true,
      }),
      makeTerminalSession({ name: "external-shell", pane_dead: true }),
      makeTerminalSession({
        name: "agent-shell",
        socket: "gobby",
        gobby_session_id: "wrong-session",
        agent_run_id: agentSession.agent_run_id,
      }),
    ];

    const joined = joinTerminalSessions(terminal, [userSession, agentSession]);

    expect(sessionKey(terminal[0])).toBe("default:user-shell");
    expect(joined).toEqual([
      {
        terminal: terminal[0],
        gobby: userSession,
        label: "other-project#7",
        refLabel: "other-project#7",
        tickerText: "",
        provider: "codex",
        paneRef: "user-shell",
        backendLabel: "gterm",
        dead: false,
        agentManaged: false,
        external: false,
      },
      {
        terminal: terminal[1],
        gobby: null,
        label: "external-shell",
        refLabel: null,
        tickerText: "external-shell",
        provider: null,
        paneRef: "external-shell",
        backendLabel: "gterm",
        dead: true,
        agentManaged: false,
        external: true,
      },
      {
        terminal: terminal[2],
        gobby: agentSession,
        label: "gobby#8",
        refLabel: "gobby#8",
        tickerText: "",
        provider: "codex",
        paneRef: "agent-shell",
        backendLabel: "gterm",
        dead: false,
        agentManaged: true,
        external: false,
      },
    ]);
    expect(findByGobbySessionId(joined, agentSession.id)).toBe(joined[2]);
    expect(findByGobbySessionId(joined, "missing")).toBeNull();
    expect(joinTerminalSessions([terminal[1]], undefined)[0].external).toBe(
      true,
    );
  });

  it("trims the current project's name off refs and keeps other projects' names", () => {
    const own = makeGobbySession({
      id: "own",
      ref: "gobby#12",
      project_id: "project-1",
    });
    const foreign = makeGobbySession({
      id: "foreign",
      ref: "other-project#7",
      project_id: "project-2",
    });
    const provisional = makeGobbySession({
      id: "provisional",
      ref: "gobby#13",
      project_id: "project-1",
    });
    const rosterBySession = new Map<string, SessionRosterLabel>([
      [
        "own",
        {
          definition: "developer",
          task: { ref: "#22203", title: "portrait fixes", claimHolder: false },
          statusLine: null,
        },
      ],
    ]);
    const joined = joinTerminalSessions(
      [
        makeTerminalSession({ name: "own", gobby_session_id: "own" }),
        makeTerminalSession({ name: "foreign", gobby_session_id: "foreign" }),
        makeTerminalSession({ name: "prov", gobby_session_id: "provisional" }),
      ],
      [own, foreign, provisional],
      "project-1",
      rosterBySession,
    );

    expect(joined[0].label).toBe("#12: developer");
    expect(joined[0].refLabel).toBe("#12");
    expect(joined[0].tickerText).toBe(
      "developer · Working task 22203 portrait fixes",
    );
    expect(joined[1].label).toBe("other-project#7");
    expect(joined[1].refLabel).toBe("other-project#7");
    // A provisional title never rides along: the ref stands alone.
    expect(joined[2].label).toBe("#13");
    expect(joined[2].tickerText).toBe("");
  });

  it("socket specific identity join", () => {
    const userSession = makeGobbySession({
      id: "user-session",
      seq_num: 10,
    });
    const agentSession = makeGobbySession({
      id: "agent-session",
      seq_num: 11,
      agent_run_id: "run-11",
    });
    const terminal = [
      makeTerminalSession({
        name: "same-name",
        socket: "default",
        gobby_session_id: userSession.id,
        agent_run_id: agentSession.agent_run_id,
        agent_managed: true,
      }),
      makeTerminalSession({
        name: "same-name",
        socket: "gobby",
        gobby_session_id: null,
        session_id: null,
        agent_run_id: agentSession.agent_run_id,
      }),
      makeTerminalSession({
        name: "pane-id-collision",
        socket: "default",
        gobby_session_id: null,
        session_id: null,
        agent_run_id: null,
        agent_managed: true,
      }),
    ];

    const joined = joinTerminalSessions(terminal, [userSession, agentSession]);

    expect(joined.map(({ terminal: row }) => sessionKey(row))).toEqual([
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

  it("titles unmanaged panes by running app, then cwd, then terminal name", () => {
    const terminal = [
      makeTerminalSession({
        name: "0",
        pane_command: "vim",
        pane_path: "/Users/dev/notes",
      }),
      makeTerminalSession({
        name: "1",
        pane_command: "zsh",
        pane_path: "/Users/dev/projects/gobby",
      }),
      makeTerminalSession({
        name: "2",
        pane_command: "-bash",
        pane_path: null,
      }),
    ];

    const joined = joinTerminalSessions(terminal, []);

    expect(joined[0].label).toBe("vim");
    expect(joined[1].label).toBe("gobby");
    expect(joined[2].label).toBe("2");
    expect(joined.map(({ paneRef }) => paneRef)).toEqual(["0", "1", "2"]);
  });

  it("derives the provider icon from the pane command for unmanaged panes", () => {
    const joined = joinTerminalSessions(
      [
        makeTerminalSession({
          name: "0",
          pane_command: "claude",
          pane_path: "/tmp",
        }),
        makeTerminalSession({
          name: "1",
          pane_command: "htop",
          pane_path: "/tmp",
        }),
      ],
      [],
    );

    expect(joined[0].provider).toBe("claude");
    expect(joined[0].label).toBe("claude");
    expect(joined[1].provider).toBeNull();
  });

  it("titles agent-managed panes without a joined session by terminal name", () => {
    const joined = joinTerminalSessions(
      [
        makeTerminalSession({
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
