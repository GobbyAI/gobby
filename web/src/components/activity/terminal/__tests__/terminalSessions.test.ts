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
  return {
    name: "shell",
    socket: "default",
    pane_pid: 123,
    pane_dead: false,
    pane_title: null,
    window_name: null,
    session_title: null,
    gobby_session_id: null,
    agent_managed: false,
    agent_run_id: null,
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
        dead: false,
        agentManaged: false,
        external: false,
      },
      {
        tmux: tmux[1],
        gobby: null,
        label: "external-shell",
        dead: true,
        agentManaged: false,
        external: true,
      },
      {
        tmux: tmux[2],
        gobby: agentSession,
        label: "#8 Agent shell",
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
        gobby_session_id: userSession.id,
        agent_run_id: agentSession.agent_run_id,
      }),
      makeTmuxSession({
        name: "pane-id-collision",
        socket: "default",
        gobby_session_id: agentSession.id,
        agent_run_id: agentSession.agent_run_id,
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
    expect(joined[2]).toMatchObject({
      gobby: null,
      label: "pane-id-collision",
      agentManaged: false,
      external: true,
    });
    expect(findByGobbySessionId(joined, agentSession.id)).toBe(joined[1]);
  });
});
