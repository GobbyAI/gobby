import { readFileSync, writeFileSync, mkdirSync } from "fs";
import { join } from "path";
import { parse, stringify } from "yaml";
import { getGobbyHome } from "./state.js";

function bootstrapPath(): string {
  return join(getGobbyHome(), "bootstrap.yaml");
}

export function readBootstrap(): Record<string, unknown> {
  try {
    const raw = readFileSync(bootstrapPath(), "utf-8");
    return (parse(raw) as Record<string, unknown>) || {};
  } catch (e: unknown) {
    if (e && typeof e === "object" && "code" in e && (e as { code: string }).code !== "ENOENT") {
      console.error("Failed to read bootstrap config:", e);
    }
    return {};
  }
}

export function writeBootstrap(data: Record<string, unknown>): void {
  const dir = getGobbyHome();
  mkdirSync(dir, { recursive: true });
  writeFileSync(bootstrapPath(), stringify(data), { mode: 0o600 });
}

/** @deprecated Use readBootstrap() instead */
export function readConfig(): Record<string, unknown> {
  return readBootstrap();
}

/** @deprecated Use writeBootstrap() instead */
export function writeConfig(data: Record<string, unknown>): void {
  writeBootstrap(data);
}

export function patchPorts(
  httpPort: number,
  wsPort: number,
  uiPort: number,
  firewallConfigured: boolean,
): void {
  const data = readBootstrap();
  data.daemon_port = httpPort;
  data.websocket_port = wsPort;
  data.ui_port = uiPort;
  if (!firewallConfigured) {
    data.bind_host = "127.0.0.1";
  }
  writeBootstrap(data);
}
