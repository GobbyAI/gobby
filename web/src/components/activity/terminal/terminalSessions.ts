import type { TmuxSession } from "../../../hooks/useTmuxSessions";
import type { GobbySession } from "../../../types/sessions";

export interface JoinedTerminalSession {
  tmux: TmuxSession;
  gobby: GobbySession | null;
  label: string;
  dead: boolean;
  agentManaged: boolean;
  external: boolean;
}

export function sessionKey(tmuxSession: TmuxSession): string {
  return `${tmuxSession.socket}:${tmuxSession.name}`;
}

function displayLabel(
  tmuxSession: TmuxSession,
  gobbySession: GobbySession | null,
): string {
  if (gobbySession === null) {
    return tmuxSession.name;
  }

  const ref = gobbySession.seq_num === null ? gobbySession.ref : `#${gobbySession.seq_num}`;
  return gobbySession.title ? `${ref} ${gobbySession.title}` : ref;
}

export function joinTmuxSessions(
  tmuxSessions: TmuxSession[],
  gobbySessions: GobbySession[] | undefined,
): JoinedTerminalSession[] {
  const byId = new Map<string, GobbySession>();
  const byAgentRunId = new Map<string, GobbySession>();

  for (const session of gobbySessions ?? []) {
    byId.set(session.id, session);
    if (session.agent_run_id !== null) {
      byAgentRunId.set(session.agent_run_id, session);
    }
  }

  return tmuxSessions.map((tmux) => {
    let gobby: GobbySession | null = null;

    if (tmux.socket === "default" && tmux.gobby_session_id !== null) {
      const candidate = byId.get(tmux.gobby_session_id);
      gobby = candidate?.agent_run_id === null ? candidate : null;
    } else if (tmux.socket === "gobby" && tmux.agent_run_id !== null) {
      gobby = byAgentRunId.get(tmux.agent_run_id) ?? null;
    }

    return {
      tmux,
      gobby,
      label: displayLabel(tmux, gobby),
      dead: tmux.pane_dead,
      agentManaged: tmux.socket === "gobby" && gobby !== null,
      external: gobby === null,
    };
  });
}

export function findByGobbySessionId(
  joined: JoinedTerminalSession[],
  sessionId: string,
): JoinedTerminalSession | null {
  return joined.find(({ gobby }) => gobby?.id === sessionId) ?? null;
}
