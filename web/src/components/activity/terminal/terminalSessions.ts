import type { TmuxSession } from "../../../hooks/useTmuxSessions";
import type { GobbySession } from "../../../types/sessions";

export interface JoinedTerminalSession {
  tmux: TmuxSession;
  gobby: GobbySession | null;
  label: string;
  provider: string | null;
  paneRef: string;
  dead: boolean;
  agentManaged: boolean;
  external: boolean;
}

// CLI providers that ship a SourceIcon mark. Matched against the joined Gobby
// session's source first, then the pane's running command.
const PROVIDER_COMMANDS = new Set([
  "claude",
  "codex",
  "droid",
  "grok",
  "qwen",
  "agy",
]);

// Commands that mean "nothing interesting is running" — an idle shell titles
// the pane by its cwd instead.
const SHELL_COMMANDS = new Set(["zsh", "bash", "sh", "fish", "tmux", "login"]);

export function sessionKey(tmuxSession: TmuxSession): string {
  return tmuxSession.terminal_id;
}

function paneDirectory(panePath: string | null): string | null {
  if (!panePath) return null;
  const trimmed = panePath.replace(/\/+$/, "");
  const base = trimmed.split("/").pop();
  return base || trimmed || null;
}

// Title priority: Gobby session ref+title, then agent run name, then the
// running app, then cwd basename, then the raw tmux session name.
function displayLabel(
  tmuxSession: TmuxSession,
  gobbySession: GobbySession | null,
): string {
  if (gobbySession !== null) {
    const ref =
      gobbySession.seq_num === null
        ? gobbySession.ref
        : `#${gobbySession.seq_num}`;
    return gobbySession.title ? `${ref} ${gobbySession.title}` : ref;
  }
  if (tmuxSession.agent_managed) {
    return tmuxSession.name;
  }
  const rawCommand = tmuxSession.pane_command;
  if (
    rawCommand &&
    !SHELL_COMMANDS.has(rawCommand.toLowerCase().replace(/^-/, ""))
  ) {
    return rawCommand;
  }
  return paneDirectory(tmuxSession.pane_path) ?? tmuxSession.name;
}

function providerFor(
  tmuxSession: TmuxSession,
  gobbySession: GobbySession | null,
): string | null {
  const source = gobbySession?.source?.toLowerCase();
  if (source && PROVIDER_COMMANDS.has(source)) return source;
  const command = tmuxSession.pane_command?.toLowerCase().replace(/^-/, "");
  if (command && PROVIDER_COMMANDS.has(command)) return command;
  return null;
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
    if (tmux.session_id !== null) {
      gobby = byId.get(tmux.session_id) ?? null;
    }
    if (gobby === null && tmux.agent_run_id !== null) {
      gobby = byAgentRunId.get(tmux.agent_run_id) ?? null;
    }

    return {
      tmux,
      gobby,
      label: displayLabel(tmux, gobby),
      provider: providerFor(tmux, gobby),
      paneRef: tmux.terminal_id,
      dead: tmux.pane_dead || tmux.state === "exited" || tmux.state === "orphaned",
      agentManaged: tmux.ownership === "gobby" && gobby !== null,
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
