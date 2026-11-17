import type { TerminalSession } from "../../../hooks/terminalRosterSnapshot";
import {
  getSessionLabelLines,
  getSessionRefLabel,
  type SessionRosterLabel,
} from "../../../lib/sessionLabel";
import type { GobbySession } from "../../../types/sessions";

export interface JoinedTerminalSession {
  terminal: TerminalSession;
  gobby: GobbySession | null;
  /** Derived head: the ref, plus `: definition` when the roster knows one. */
  label: string;
  /** Static row prefix (`#12856`); null for panes without a Gobby session. */
  refLabel: string | null;
  /** Derived detail (definition · task or status line) that may ticker when it overflows the row. */
  tickerText: string;
  provider: string | null;
  paneRef: string;
  /** Native terminal host label. */
  backendLabel: "gterm";
  dead: boolean;
  agentManaged: boolean;
  external: boolean;
}

// CLI providers that ship a SourceIcon mark. Matched against the joined Gobby
// session's source first, then the pane's running command.
const PROVIDER_COMMANDS = new Set(["claude", "codex", "droid", "grok", "agy"]);

// Commands that mean "nothing interesting is running" — an idle shell titles
// the pane by its cwd instead.
const SHELL_COMMANDS = new Set(["zsh", "bash", "sh", "fish", "login"]);

export function sessionKey(terminalSession: TerminalSession): string {
  return terminalSession.terminal_id;
}

function paneDirectory(panePath: string | null): string | null {
  if (!panePath) return null;
  const trimmed = panePath.replace(/\/+$/, "");
  const base = trimmed.split("/").pop();
  return base || trimmed || null;
}

// Title priority: Gobby session ref+title, then agent run name, then the
// running app, then cwd basename, then the raw terminal session name.
type DisplayParts = Pick<
  JoinedTerminalSession,
  "label" | "refLabel" | "tickerText"
>;

function displayParts(
  terminalSession: TerminalSession,
  gobbySession: GobbySession | null,
  currentProjectId: string | null,
  rosterBySession: ReadonlyMap<string, SessionRosterLabel>,
): DisplayParts {
  if (gobbySession !== null) {
    const trimProject =
      currentProjectId !== null && gobbySession.project_id === currentProjectId;
    const roster = rosterBySession.get(gobbySession.id);
    const lines = getSessionLabelLines(gobbySession, roster, trimProject);
    const definition = roster?.definition?.trim();
    const detail = lines.taskLine ?? lines.statusLine;
    return {
      label: lines.head,
      refLabel: getSessionRefLabel(gobbySession.ref, trimProject),
      tickerText: [definition, detail].filter(Boolean).join(" · "),
    };
  }
  const label = externalLabel(terminalSession);
  return { label, refLabel: null, tickerText: label };
}

function externalLabel(terminalSession: TerminalSession): string {
  if (terminalSession.agent_managed) {
    return terminalSession.name;
  }
  const rawCommand = terminalSession.pane_command;
  if (
    rawCommand &&
    !SHELL_COMMANDS.has(rawCommand.toLowerCase().replace(/^-/, ""))
  ) {
    return rawCommand;
  }
  return paneDirectory(terminalSession.pane_path) ?? terminalSession.name;
}

function providerFor(
  terminalSession: TerminalSession,
  gobbySession: GobbySession | null,
): string | null {
  const source = gobbySession?.source?.toLowerCase();
  if (source && PROVIDER_COMMANDS.has(source)) return source;
  const command = terminalSession.pane_command?.toLowerCase().replace(/^-/, "");
  if (command && PROVIDER_COMMANDS.has(command)) return command;
  return null;
}

/**
 * Join terminal panes to their Gobby sessions. `currentProjectId` trims that
 * project's name off session refs (`gobby#12` → `#12`) because the panel is
 * already scoped to it; refs from other projects keep their name.
 */
export function joinTerminalSessions(
  terminalSessions: TerminalSession[],
  gobbySessions: GobbySession[] | undefined,
  currentProjectId: string | null = null,
  rosterBySession: ReadonlyMap<string, SessionRosterLabel> = new Map(),
): JoinedTerminalSession[] {
  const byId = new Map<string, GobbySession>();
  const byAgentRunId = new Map<string, GobbySession>();

  for (const session of gobbySessions ?? []) {
    byId.set(session.id, session);
    if (session.agent_run_id !== null) {
      byAgentRunId.set(session.agent_run_id, session);
    }
  }

  return terminalSessions.map((terminal) => {
    let gobby: GobbySession | null = null;
    if (terminal.session_id !== null) {
      gobby = byId.get(terminal.session_id) ?? null;
    }
    if (gobby === null && terminal.agent_run_id !== null) {
      gobby = byAgentRunId.get(terminal.agent_run_id) ?? null;
    }

    const parts = displayParts(
      terminal,
      gobby,
      currentProjectId,
      rosterBySession,
    );
    return {
      terminal,
      gobby,
      label: parts.label,
      refLabel: parts.refLabel,
      tickerText: parts.tickerText,
      provider: providerFor(terminal, gobby),
      paneRef: terminal.name,
      backendLabel: "gterm",
      dead:
        terminal.pane_dead ||
        terminal.state === "exited" ||
        terminal.state === "orphaned",
      agentManaged: terminal.ownership === "gobby" && gobby !== null,
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
