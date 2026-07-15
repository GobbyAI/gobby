import { readFileSync, writeFileSync, mkdirSync } from "fs";
import { homedir } from "os";
import { join } from "path";

export interface SetupState {
  version: number;
  started_at: string;
  completed_at: string | null;
  completed_step_id: string | null;
  user_name: string | null;
  ports: { http: number; ws: number; ui: number };
  detected_tools: Record<string, boolean>;
  tool_versions: Record<string, string>;
  installed_clis: string[];
  projects: string[];
  firewall_configured: boolean;
  tailscale_configured: boolean;
  secrets_configured: string[];
  falkordb_installed: boolean;
  falkordb_password_set: boolean;
  personal_dir_created: boolean;
  desktop_shortcut_created: boolean;
}

function createDefaultState(): SetupState {
  return {
    version: 3,
    started_at: new Date().toISOString(),
    completed_at: null,
    completed_step_id: null,
    user_name: null,
    ports: { http: 60887, ws: 60888, ui: 60889 },
    detected_tools: {},
    tool_versions: {},
    installed_clis: [],
    projects: [],
    firewall_configured: false,
    tailscale_configured: false,
    secrets_configured: [],
    falkordb_installed: false,
    falkordb_password_set: false,
    personal_dir_created: false,
    desktop_shortcut_created: false,
  };
}

export function getGobbyHome(): string {
  return process.env.GOBBY_HOME || join(homedir(), ".gobby");
}

function statePath(): string {
  return join(getGobbyHome(), "setup_state.json");
}

export function loadState(): SetupState {
  try {
    const raw = readFileSync(statePath(), "utf-8");
    const parsed = JSON.parse(raw);
    return { ...createDefaultState(), ...parsed };
  } catch {
    return createDefaultState();
  }
}

export function saveState(state: SetupState): void {
  const dir = getGobbyHome();
  mkdirSync(dir, { recursive: true });
  writeFileSync(statePath(), JSON.stringify(state, null, 2));
}
