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

/** Step ID map for migrating v1 (numeric) state to v2 (string IDs). */
const V1_STEP_MAP: Record<number, string> = {
  1: "welcome",
  2: "about-you",
  3: "syscheck",
  4: "config",
  5: "firewall",
  6: "tailscale",
  7: "projects",
  8: "hooks",
  9: "services",
  10: "personal",
  11: "launch",
};

export function getGobbyHome(): string {
  return process.env.GOBBY_HOME || join(homedir(), ".gobby");
}

function statePath(): string {
  return join(getGobbyHome(), "setup_state.json");
}

type PersistedSetupState = Partial<SetupState> & {
  completed_step?: unknown;
  neo4j_installed?: unknown;
  neo4j_password_set?: unknown;
};

function migrateState(raw: PersistedSetupState): { state: SetupState; migrated: boolean } {
  let migrated = false;

  // Migrate v1 numeric completed_step -> v2 string completed_step_id.
  if (raw.version === 1 && typeof raw.completed_step === "number") {
    raw.completed_step_id = V1_STEP_MAP[raw.completed_step] || null;
    delete raw.completed_step;
    migrated = true;
  }

  if ("neo4j_installed" in raw) {
    raw.falkordb_installed = false;
    delete raw.neo4j_installed;
    migrated = true;
  }

  if ("neo4j_password_set" in raw) {
    raw.falkordb_password_set = false;
    delete raw.neo4j_password_set;
    migrated = true;
  }

  if (raw.version !== 3) {
    raw.version = 3;
    migrated = true;
  }

  return { state: { ...createDefaultState(), ...raw }, migrated };
}

export function loadState(): SetupState {
  try {
    const raw = readFileSync(statePath(), "utf-8");
    const parsed = JSON.parse(raw);
    const { state, migrated } = migrateState(parsed);

    if (migrated) {
      try {
        saveState(state);
      } catch {
        /* Migration persistence is best-effort; callers can still use migrated state. */
      }
    }

    return state;
  } catch {
    return createDefaultState();
  }
}

export function saveState(state: SetupState): void {
  const dir = getGobbyHome();
  mkdirSync(dir, { recursive: true });
  writeFileSync(statePath(), JSON.stringify(state, null, 2));
}
