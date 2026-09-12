import type { TmuxSession } from "../../../hooks/useTmuxSessions";
import { getActivitySessionTitleParts } from "../../../lib/sessionTitle";
import type { GobbySession } from "../../../types/sessions";

export interface JoinedTerminalSession {
  tmux: TmuxSession;
  gobby: GobbySession | null;
  /** Full display label: `refLabel: titleText` for Gobby sessions. */
  label: string;
  /** Static row prefix (`#12856`); null for panes without a Gobby session. */
  refLabel: string | null;
  /** The part of the label that may ticker when it overflows the row. */
  titleText: string;
  provider: string | null;
  paneRef: string;
  /** Terminal backend as the product names it: `tmux` or `gterm`. */
  backendLabel: "tmux" | "gterm";
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
type DisplayParts = Pick<
  JoinedTerminalSession,
  "label" | "refLabel" | "titleText"
>;

function displayParts(
  tmuxSession: TmuxSession,
  gobbySession: GobbySession | null,
  currentProjectId: string | null,
): DisplayParts {
  if (gobbySession !== null) {
    const { ref, title } = getActivitySessionTitleParts(
      gobbySession,
      currentProjectId !== null && gobbySession.project_id === currentProjectId,
    );
    return {
      label: title === null ? ref : `${ref}: ${title}`,
      refLabel: ref,
      titleText: title ?? "",
    };
  }
  const label = externalLabel(tmuxSession);
  return { label, refLabel: null, titleText: label };
}

function externalLabel(tmuxSession: TmuxSession): string {
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

/**
 * Join tmux panes to their Gobby sessions. `currentProjectId` trims that
 * project's name off session refs (`gobby#12` → `#12`) because the panel is
 * already scoped to it; refs from other projects keep their name.
 */
export function joinTmuxSessions(
  tmuxSessions: TmuxSession[],
  gobbySessions: GobbySession[] | undefined,
  currentProjectId: string | null = null,
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

    const parts = displayParts(tmux, gobby, currentProjectId);
    return {
      tmux,
      gobby,
      label: parts.label,
      refLabel: parts.refLabel,
      titleText: parts.titleText,
      provider: providerFor(tmux, gobby),
      paneRef: tmux.name,
      backendLabel: tmux.backend === "native" ? "gterm" : "tmux",
      dead:
        tmux.pane_dead || tmux.state === "exited" || tmux.state === "orphaned",
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
